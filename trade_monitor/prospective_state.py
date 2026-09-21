from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .strategy_catalog import EXECUTION_STYLES, STRATEGY_METHODS


PROSPECTIVE_CONTEXT_VERSION = 1
MARKET_BIASES = {"BULL_PRIMARY", "BEAR_PRIMARY", "RANGE_PRIMARY", "BALANCED", "UNDEFINED"}
HYPOTHESIS_DIRECTIONS = {"BULL", "BEAR", "RANGE", "UNDEFINED"}
HYPOTHESIS_STATUSES = {
    "POTENTIAL",
    "STRENGTHENING",
    "NEAR_CONFIRMATION",
    "CONFIRMED",
    "DEGRADED",
    "CANCELLED",
    "UNDEFINED",
}
HYPOTHESIS_CONFIDENCE = {"HIGH", "MEDIUM", "LOW", "UNAVAILABLE"}
SUPPORTING_METHODS = {
    "OPENING_RANGE",
    "NUMBER_BOARD",
    "BOX",
    "PIVOT",
    "DOW_STRUCTURE",
    "LEFT_RIGHT",
    "ANCHOR",
    "TAIJI",
    "YIZHI",
    "QUADRANT",
    "CCLASS",
    "XPROCESS",
    "ORIGINAL_PATTERN",
}
PLAYBOOK_STATUSES = {"WAITING_STRUCTURE", "WATCHING", "ARMED", "PROHIBITED", "UNAVAILABLE"}
TAIJI_SEQUENCES = {"NONE", "ANCHOR_1", "CORRECTION_2", "COPY_3", "CORRECTION_4", "COPY_5", "POST_5"}
TAIJI_ROLES = {"NONE", "ANCHOR", "CORRECTION", "COPY", "POST_TREND"}
TAIJI_OUTCOMES = {"STRONG", "ACCEPTABLE", "WEAK", "FAILED", "UNKNOWN"}
AMPLITUDE_TRENDS = {"EXPANDING", "STABLE", "CONTRACTING", "UNDEFINED"}
DURATION_TRENDS = {"LONGER", "STABLE", "SHORTER", "UNDEFINED"}
TAIJI_ASSESSMENTS = {"STRENGTHENING", "HEALTHY", "WEAKENING", "REVERSAL_RISK", "UNDEFINED"}
PATTERN_TYPES = STRATEGY_METHODS
ZONE_RELIABILITY = {"DIRECT", "ESTIMATED", "UNAVAILABLE"}

PROSPECTIVE_KEYS = {
    "version",
    "as_of",
    "market_bias",
    "primary_hypothesis",
    "alternative_hypothesis",
    "long_playbook",
    "short_playbook",
    "taiji_evolution",
    "assessment_changed_at",
    "notes",
}


class ProspectiveStateError(ValueError):
    pass


def empty_prospective_context(*, as_of: str) -> dict[str, Any]:
    timestamp = _iso_time(as_of, "as_of")
    return {
        "version": PROSPECTIVE_CONTEXT_VERSION,
        "as_of": timestamp,
        "market_bias": "UNDEFINED",
        "primary_hypothesis": _empty_hypothesis("尚無可驗證的主要演化情境。"),
        "alternative_hypothesis": _empty_hypothesis("尚無可驗證的備用演化情境。"),
        "long_playbook": _empty_playbook("LONG"),
        "short_playbook": _empty_playbook("SHORT"),
        "taiji_evolution": _empty_taiji_evolution(),
        "assessment_changed_at": None,
        "notes": ["前瞻情境尚未建立；不得為了提供方向而硬湊交易。"],
    }


def validate_prospective_context(value: Any, *, as_of: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != PROSPECTIVE_KEYS:
        raise ProspectiveStateError("prospective_context has invalid fields")
    ceiling_text = _iso_time(as_of, "as_of")
    own_as_of = _iso_time(value.get("as_of"), "prospective_context.as_of")
    if own_as_of != ceiling_text:
        raise ProspectiveStateError("prospective_context.as_of must match market_structure_state.as_of")
    version = value.get("version")
    if version != PROSPECTIVE_CONTEXT_VERSION:
        raise ProspectiveStateError("prospective_context.version is invalid")

    bias = _enum(value.get("market_bias"), MARKET_BIASES, "market_bias")
    primary = _hypothesis(value.get("primary_hypothesis"), as_of=own_as_of, field="primary_hypothesis")
    alternative = _hypothesis(
        value.get("alternative_hypothesis"), as_of=own_as_of, field="alternative_hypothesis"
    )
    long_playbook = _playbook(value.get("long_playbook"), "LONG", "long_playbook")
    short_playbook = _playbook(value.get("short_playbook"), "SHORT", "short_playbook")
    taiji_evolution = _taiji_evolution(value.get("taiji_evolution"))
    changed_at = _optional_time(value.get("assessment_changed_at"), "assessment_changed_at")
    notes = _text_list(value.get("notes"), "prospective_context.notes", minimum=1, maximum=4, item_max=240)

    if bias == "UNDEFINED":
        if primary["status"] != "UNDEFINED" or changed_at is not None:
            raise ProspectiveStateError("undefined market bias requires an undefined primary hypothesis")
    else:
        if primary["status"] == "UNDEFINED":
            raise ProspectiveStateError("an active market bias requires a primary hypothesis")
        if changed_at is None or _parse_time(changed_at, "assessment_changed_at") > _parse_time(own_as_of, "as_of"):
            raise ProspectiveStateError("an active market bias requires a causal assessment_changed_at")

    expected_direction = {
        "BULL_PRIMARY": "BULL",
        "BEAR_PRIMARY": "BEAR",
        "RANGE_PRIMARY": "RANGE",
    }.get(bias)
    if expected_direction is not None and primary["direction"] != expected_direction:
        raise ProspectiveStateError("primary hypothesis direction does not match market bias")
    if primary["status"] != "UNDEFINED" and alternative["status"] != "UNDEFINED":
        if primary["direction"] == alternative["direction"]:
            raise ProspectiveStateError("primary and alternative hypotheses must represent different paths")
        if primary["hypothesis_id"] == alternative["hypothesis_id"]:
            raise ProspectiveStateError("primary and alternative hypothesis ids must differ")

    return {
        "version": version,
        "as_of": own_as_of,
        "market_bias": bias,
        "primary_hypothesis": primary,
        "alternative_hypothesis": alternative,
        "long_playbook": long_playbook,
        "short_playbook": short_playbook,
        "taiji_evolution": taiji_evolution,
        "assessment_changed_at": changed_at,
        "notes": notes,
    }


def validate_prospective_transition(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    prior_as_of: str,
    current_as_of: str,
) -> None:
    before = validate_prospective_context(previous, as_of=prior_as_of)
    after = validate_prospective_context(current, as_of=current_as_of)
    prior_time = _parse_time(prior_as_of, "prior_as_of")

    before_signature = _assessment_signature(before)
    after_signature = _assessment_signature(after)
    if before_signature == after_signature:
        if before["assessment_changed_at"] != after["assessment_changed_at"]:
            raise ProspectiveStateError("unchanged prospective assessment must preserve assessment_changed_at")
    elif after["market_bias"] == "UNDEFINED":
        if after["assessment_changed_at"] is not None:
            raise ProspectiveStateError("reset prospective assessment must clear assessment_changed_at")
    elif _parse_time(after["assessment_changed_at"], "assessment_changed_at") <= prior_time:
        raise ProspectiveStateError("changed prospective assessment must use a new causal timestamp")

    old_hypotheses = {
        item["hypothesis_id"]: item
        for item in (before["primary_hypothesis"], before["alternative_hypothesis"])
        if item["hypothesis_id"] is not None
    }
    for item in (after["primary_hypothesis"], after["alternative_hypothesis"]):
        hypothesis_id = item["hypothesis_id"]
        if hypothesis_id is None:
            continue
        old = old_hypotheses.get(hypothesis_id)
        if old is None:
            if _parse_time(item["first_seen_at"], "first_seen_at") <= prior_time:
                raise ProspectiveStateError("new hypotheses cannot backdate first_seen_at")
            continue
        if old["direction"] != item["direction"] or old["first_seen_at"] != item["first_seen_at"]:
            raise ProspectiveStateError("a hypothesis cannot change direction or first_seen_at")
        if old["status"] == item["status"]:
            if old["status_changed_at"] != item["status_changed_at"]:
                raise ProspectiveStateError("unchanged hypothesis status must preserve status_changed_at")
        elif _parse_time(item["status_changed_at"], "status_changed_at") <= prior_time:
            raise ProspectiveStateError("changed hypothesis status must use a new causal timestamp")


def prospective_notification_reasons(previous: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    if previous["market_bias"] != current["market_bias"]:
        reasons.append("主要盤勢推演改變")
    for key, label in (
        ("primary_hypothesis", "主要情境階段改變"),
        ("alternative_hypothesis", "備用情境階段改變"),
    ):
        before = previous[key]
        after = current[key]
        if any(
            before[field] != after[field]
            for field in ("hypothesis_id", "direction", "status", "confidence")
        ):
            reasons.append(label)
    for key, label in (("long_playbook", "偏多預案更新"), ("short_playbook", "偏空預案更新")):
        before = previous[key]
        after = current[key]
        if any(
            before[field] != after[field]
            for field in (
                "status",
                "mapped_pattern",
                "execution_style",
                "observation_zone",
                "trigger_zone",
                "invalidation_zone",
                "first_obstacle_zone",
            )
        ):
            reasons.append(label)
    if any(
        previous["taiji_evolution"][field] != current["taiji_evolution"][field]
        for field in (
            "active_sequence",
            "copy_outcomes",
            "correction_outcomes",
            "copy_amplitude_trend",
            "copy_duration_trend",
            "correction_amplitude_trend",
            "correction_duration_trend",
            "structural_assessment",
        )
    ):
        reasons.append("太極複製與修正演化改變")
    return reasons


def _empty_hypothesis(reason: str) -> dict[str, Any]:
    return {
        "hypothesis_id": None,
        "direction": "UNDEFINED",
        "status": "UNDEFINED",
        "confidence": "UNAVAILABLE",
        "title": reason,
        "thesis": reason,
        "supporting_methods": [],
        "supporting_evidence": [],
        "conflicting_evidence": [],
        "confirmation_conditions": ["等待可驗證結構。"],
        "downgrade_conditions": ["資料仍不足。"],
        "cancellation_conditions": ["尚無作用中假設。"],
        "next_scenario": "等待新的大小結構、定錨或邊界事件。",
        "first_seen_at": None,
        "status_changed_at": None,
    }


def _hypothesis(value: Any, *, as_of: str, field: str) -> dict[str, Any]:
    keys = {
        "hypothesis_id",
        "direction",
        "status",
        "confidence",
        "title",
        "thesis",
        "supporting_methods",
        "supporting_evidence",
        "conflicting_evidence",
        "confirmation_conditions",
        "downgrade_conditions",
        "cancellation_conditions",
        "next_scenario",
        "first_seen_at",
        "status_changed_at",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ProspectiveStateError(f"{field} has invalid fields")
    direction = _enum(value.get("direction"), HYPOTHESIS_DIRECTIONS, f"{field}.direction")
    status = _enum(value.get("status"), HYPOTHESIS_STATUSES, f"{field}.status")
    confidence = _enum(value.get("confidence"), HYPOTHESIS_CONFIDENCE, f"{field}.confidence")
    hypothesis_id = _optional_text(value.get("hypothesis_id"), f"{field}.hypothesis_id", 120)
    first_seen = _optional_time(value.get("first_seen_at"), f"{field}.first_seen_at")
    status_changed = _optional_time(value.get("status_changed_at"), f"{field}.status_changed_at")
    methods_value = value.get("supporting_methods")
    if not isinstance(methods_value, list) or len(methods_value) > 13:
        raise ProspectiveStateError(f"{field}.supporting_methods must contain at most thirteen items")
    methods = [_enum(item, SUPPORTING_METHODS, f"{field}.supporting_methods item") for item in methods_value]
    if len(methods) != len(set(methods)):
        raise ProspectiveStateError(f"{field}.supporting_methods must not contain duplicates")

    if status == "UNDEFINED":
        if direction != "UNDEFINED" or confidence != "UNAVAILABLE" or hypothesis_id is not None:
            raise ProspectiveStateError(f"{field} undefined hypothesis is inconsistent")
        if first_seen is not None or status_changed is not None or methods:
            raise ProspectiveStateError(f"{field} undefined hypothesis cannot carry causal state")
    else:
        if direction == "UNDEFINED" or confidence == "UNAVAILABLE" or hypothesis_id is None:
            raise ProspectiveStateError(f"{field} active hypothesis requires direction, confidence and id")
        if first_seen is None or status_changed is None:
            raise ProspectiveStateError(f"{field} active hypothesis requires causal timestamps")
        ceiling = _parse_time(as_of, "as_of")
        first_time = _parse_time(first_seen, f"{field}.first_seen_at")
        changed_time = _parse_time(status_changed, f"{field}.status_changed_at")
        if first_time > changed_time or changed_time > ceiling:
            raise ProspectiveStateError(f"{field} hypothesis timestamps are not causal")
        if not methods:
            raise ProspectiveStateError(f"{field} active hypothesis requires supporting methods")

    return {
        "hypothesis_id": hypothesis_id,
        "direction": direction,
        "status": status,
        "confidence": confidence,
        "title": _text(value.get("title"), f"{field}.title", 100),
        "thesis": _text(value.get("thesis"), f"{field}.thesis", 360),
        "supporting_methods": methods,
        "supporting_evidence": _text_list(
            value.get("supporting_evidence"), f"{field}.supporting_evidence", minimum=0, maximum=5, item_max=220
        ),
        "conflicting_evidence": _text_list(
            value.get("conflicting_evidence"), f"{field}.conflicting_evidence", minimum=0, maximum=5, item_max=220
        ),
        "confirmation_conditions": _text_list(
            value.get("confirmation_conditions"), f"{field}.confirmation_conditions", minimum=1, maximum=4, item_max=260
        ),
        "downgrade_conditions": _text_list(
            value.get("downgrade_conditions"), f"{field}.downgrade_conditions", minimum=1, maximum=4, item_max=260
        ),
        "cancellation_conditions": _text_list(
            value.get("cancellation_conditions"), f"{field}.cancellation_conditions", minimum=1, maximum=4, item_max=260
        ),
        "next_scenario": _text(value.get("next_scenario"), f"{field}.next_scenario", 300),
        "first_seen_at": first_seen,
        "status_changed_at": status_changed,
    }


def _empty_playbook(direction: str) -> dict[str, Any]:
    label = "偏多" if direction == "LONG" else "偏空"
    return {
        "direction": direction,
        "status": "UNAVAILABLE",
        "mapped_pattern": "NONE",
        "execution_style": "NOT_APPLICABLE",
        "method_selection_reason": f"{label}預案尚未選定可執行戰法。",
        "observation_zone": _empty_zone(),
        "trigger_zone": _empty_zone(),
        "invalidation_zone": _empty_zone(),
        "first_obstacle_zone": _empty_zone(),
        "required_k_behavior": f"{label}預案尚缺可辨識結構。",
        "close_trigger": "尚無可驗證的已收盤觸發。",
        "next_bar_entry": "目前不得評估進場。",
        "structural_stop": "尚無法定義同級結構停損。",
        "no_chase": "沒有完整觸發與回測時不得追價。",
        "expected_behavior": "尚無作用中進場預案。",
        "max_wait_bars": None,
        "behavior_invalidation": "尚無作用中進場預案。",
        "exit_plan": "無持倉時不建立出場事件。",
        "switch_condition": "等待相反方向取得結構控制權。",
    }


def _empty_taiji_evolution() -> dict[str, Any]:
    return {
        "active_sequence": "NONE",
        "active_role": "NONE",
        "active_direction": None,
        "copy_outcomes": [],
        "correction_outcomes": [],
        "copy_amplitude_trend": "UNDEFINED",
        "copy_duration_trend": "UNDEFINED",
        "correction_amplitude_trend": "UNDEFINED",
        "correction_duration_trend": "UNDEFINED",
        "structural_assessment": "UNDEFINED",
        "interpretation": "尚無足夠太極複製與修正序列可比較。",
        "continuation_condition": "等待至少一組因果完成的複製與修正。",
        "regime_change_condition": "尚無作用中太極結構可定義變盤條件。",
    }


def _taiji_evolution(value: Any) -> dict[str, Any]:
    keys = {
        "active_sequence",
        "active_role",
        "active_direction",
        "copy_outcomes",
        "correction_outcomes",
        "copy_amplitude_trend",
        "copy_duration_trend",
        "correction_amplitude_trend",
        "correction_duration_trend",
        "structural_assessment",
        "interpretation",
        "continuation_condition",
        "regime_change_condition",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ProspectiveStateError("taiji_evolution has invalid fields")
    sequence = _enum(value.get("active_sequence"), TAIJI_SEQUENCES, "taiji_evolution.active_sequence")
    role = _enum(value.get("active_role"), TAIJI_ROLES, "taiji_evolution.active_role")
    direction_value = value.get("active_direction")
    direction = None if direction_value is None else _enum(
        direction_value, {"BULL", "BEAR"}, "taiji_evolution.active_direction"
    )
    if sequence == "NONE":
        if role != "NONE" or direction is not None:
            raise ProspectiveStateError("inactive taiji evolution must clear role and direction")
    elif role == "NONE" or direction is None:
        raise ProspectiveStateError("active taiji evolution requires role and direction")
    copies = _enum_list(value.get("copy_outcomes"), TAIJI_OUTCOMES, "taiji_evolution.copy_outcomes", 4)
    corrections = _enum_list(
        value.get("correction_outcomes"), TAIJI_OUTCOMES, "taiji_evolution.correction_outcomes", 4
    )
    copy_amplitude = _enum(
        value.get("copy_amplitude_trend"), AMPLITUDE_TRENDS, "taiji_evolution.copy_amplitude_trend"
    )
    copy_duration = _enum(
        value.get("copy_duration_trend"), DURATION_TRENDS, "taiji_evolution.copy_duration_trend"
    )
    correction_amplitude = _enum(
        value.get("correction_amplitude_trend"),
        AMPLITUDE_TRENDS,
        "taiji_evolution.correction_amplitude_trend",
    )
    correction_duration = _enum(
        value.get("correction_duration_trend"),
        DURATION_TRENDS,
        "taiji_evolution.correction_duration_trend",
    )
    if len(copies) < 2 and (copy_amplitude != "UNDEFINED" or copy_duration != "UNDEFINED"):
        raise ProspectiveStateError("copy trends require at least two retained copy outcomes")
    if len(corrections) < 2 and (
        correction_amplitude != "UNDEFINED" or correction_duration != "UNDEFINED"
    ):
        raise ProspectiveStateError("correction trends require at least two retained correction outcomes")
    assessment = _enum(
        value.get("structural_assessment"), TAIJI_ASSESSMENTS, "taiji_evolution.structural_assessment"
    )
    if sequence == "NONE" and assessment != "UNDEFINED":
        # With no causal Taiji leg there is no Taiji assessment to persist.
        # Narrative text may still discuss reversal risk, but the machine field
        # must remain neutral and can be repaired without changing the analysis.
        assessment = "UNDEFINED"
    return {
        "active_sequence": sequence,
        "active_role": role,
        "active_direction": direction,
        "copy_outcomes": copies,
        "correction_outcomes": corrections,
        "copy_amplitude_trend": copy_amplitude,
        "copy_duration_trend": copy_duration,
        "correction_amplitude_trend": correction_amplitude,
        "correction_duration_trend": correction_duration,
        "structural_assessment": assessment,
        "interpretation": _text(value.get("interpretation"), "taiji_evolution.interpretation", 360),
        "continuation_condition": _text(
            value.get("continuation_condition"), "taiji_evolution.continuation_condition", 300
        ),
        "regime_change_condition": _text(
            value.get("regime_change_condition"), "taiji_evolution.regime_change_condition", 300
        ),
    }


def validate_taiji_evolution_alignment(
    prospective_context: Mapping[str, Any], cclass_context: Mapping[str, Any]
) -> None:
    evolution = prospective_context["taiji_evolution"]
    taiji = cclass_context["taiji_context"]
    by_id = {item["leg_id"]: item for item in taiji["legs"]}
    active = by_id.get(taiji["active_leg_id"])
    if active is None:
        if evolution["active_sequence"] != "NONE":
            raise ProspectiveStateError("taiji evolution cannot invent an active leg")
    else:
        if any(
            (
                evolution["active_sequence"] != active["sequence"],
                evolution["active_role"] != active["role"],
                evolution["active_direction"] != active["direction"],
            )
        ):
            raise ProspectiveStateError("taiji evolution must match the active causal leg")
    expected_copies = [
        item["copy_quality"] for item in taiji["legs"] if item["role"] == "COPY"
    ][-4:]
    expected_corrections = [
        item["correction_quality"] for item in taiji["legs"] if item["role"] == "CORRECTION"
    ][-4:]
    if evolution["copy_outcomes"] != expected_copies:
        raise ProspectiveStateError("taiji copy outcome history must match retained causal legs")
    if evolution["correction_outcomes"] != expected_corrections:
        raise ProspectiveStateError("taiji correction outcome history must match retained causal legs")


def _playbook(value: Any, expected_direction: str, field: str) -> dict[str, Any]:
    keys = {
        "direction",
        "status",
        "mapped_pattern",
        "execution_style",
        "method_selection_reason",
        "observation_zone",
        "trigger_zone",
        "invalidation_zone",
        "first_obstacle_zone",
        "required_k_behavior",
        "close_trigger",
        "next_bar_entry",
        "structural_stop",
        "no_chase",
        "expected_behavior",
        "max_wait_bars",
        "behavior_invalidation",
        "exit_plan",
        "switch_condition",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ProspectiveStateError(f"{field} has invalid fields")
    direction = _enum(value.get("direction"), {expected_direction}, f"{field}.direction")
    status = _enum(value.get("status"), PLAYBOOK_STATUSES, f"{field}.status")
    pattern = _enum(value.get("mapped_pattern"), PATTERN_TYPES, f"{field}.mapped_pattern")
    execution_style = _enum(
        value.get("execution_style"), EXECUTION_STYLES, f"{field}.execution_style"
    )
    zones = {
        key: _price_zone(value.get(key), f"{field}.{key}")
        for key in ("observation_zone", "trigger_zone", "invalidation_zone", "first_obstacle_zone")
    }
    max_wait = value.get("max_wait_bars")
    if max_wait is not None and (isinstance(max_wait, bool) or not isinstance(max_wait, int) or not 1 <= max_wait <= 10):
        raise ProspectiveStateError(f"{field}.max_wait_bars must be null or an integer from one to ten")
    if status == "ARMED":
        if pattern == "NONE":
            raise ProspectiveStateError(f"{field} armed playbook requires a pattern")
        if any(zones[key]["reliability"] == "UNAVAILABLE" for key in ("trigger_zone", "invalidation_zone")):
            # ARMED means the next completed bar can execute a fully specified
            # plan.  If the trigger or stop structure has not formed, preserve
            # the chart analysis but conservatively keep the playbook watching.
            # This normalization can only remove execution eligibility.
            status = "WATCHING"
        if execution_style == "NOT_APPLICABLE":
            raise ProspectiveStateError(f"{field} armed playbook requires an execution style")
        if max_wait is None:
            raise ProspectiveStateError(f"{field} armed playbook requires max_wait_bars")
    if pattern == "NONE" and execution_style != "NOT_APPLICABLE":
        raise ProspectiveStateError(f"{field} cannot choose an execution style without a strategy method")
    return {
        "direction": direction,
        "status": status,
        "mapped_pattern": pattern,
        "execution_style": execution_style,
        "method_selection_reason": _text(
            value.get("method_selection_reason"), f"{field}.method_selection_reason", 300
        ),
        **zones,
        "required_k_behavior": _text(value.get("required_k_behavior"), f"{field}.required_k_behavior", 300),
        "close_trigger": _text(value.get("close_trigger"), f"{field}.close_trigger", 300),
        "next_bar_entry": _text(value.get("next_bar_entry"), f"{field}.next_bar_entry", 260),
        "structural_stop": _text(value.get("structural_stop"), f"{field}.structural_stop", 260),
        "no_chase": _text(value.get("no_chase"), f"{field}.no_chase", 240),
        "expected_behavior": _text(value.get("expected_behavior"), f"{field}.expected_behavior", 300),
        "max_wait_bars": max_wait,
        "behavior_invalidation": _text(value.get("behavior_invalidation"), f"{field}.behavior_invalidation", 300),
        "exit_plan": _text(value.get("exit_plan"), f"{field}.exit_plan", 300),
        "switch_condition": _text(value.get("switch_condition"), f"{field}.switch_condition", 300),
    }


def _empty_zone() -> dict[str, Any]:
    return {
        "low": None,
        "high": None,
        "reliability": "UNAVAILABLE",
        "reason": "圖面不足，暫不提供可靠區間。",
    }


def _price_zone(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"low", "high", "reliability", "reason"}:
        raise ProspectiveStateError(f"{field} has invalid fields")
    reliability = _enum(value.get("reliability"), ZONE_RELIABILITY, f"{field}.reliability")
    low = _optional_positive_number(value.get("low"), f"{field}.low")
    high = _optional_positive_number(value.get("high"), f"{field}.high")
    if reliability == "UNAVAILABLE":
        if low is not None or high is not None:
            raise ProspectiveStateError(f"{field} unavailable zone cannot contain prices")
    elif low is None or high is None or low > high:
        raise ProspectiveStateError(f"{field} available zone must contain an ordered range")
    return {
        "low": low,
        "high": high,
        "reliability": reliability,
        "reason": _text(value.get("reason"), f"{field}.reason", 180),
    }


def _assessment_signature(value: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        value["market_bias"],
        value["primary_hypothesis"]["hypothesis_id"],
        value["primary_hypothesis"]["status"],
        value["primary_hypothesis"]["confidence"],
        value["alternative_hypothesis"]["hypothesis_id"],
        value["alternative_hypothesis"]["status"],
        value["alternative_hypothesis"]["confidence"],
    )


def _enum(value: Any, allowed: set[str], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ProspectiveStateError(f"{field} is invalid")
    return value


def _enum_list(value: Any, allowed: set[str], field: str, maximum: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ProspectiveStateError(f"{field} must contain at most {maximum} items")
    return [_enum(item, allowed, f"{field} item") for item in value]


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ProspectiveStateError(f"{field} must be text")
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > maximum:
        raise ProspectiveStateError(f"{field} must contain one to {maximum} characters")
    return cleaned


def _optional_text(value: Any, field: str, maximum: int) -> str | None:
    return None if value is None else _text(value, field, maximum)


def _text_list(value: Any, field: str, *, minimum: int, maximum: int, item_max: int) -> list[str]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ProspectiveStateError(f"{field} must contain {minimum} to {maximum} items")
    return [_text(item, f"{field} item", item_max) for item in value]


def _optional_positive_number(value: Any, field: str) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ProspectiveStateError(f"{field} must be a positive number or null")
    return value


def _iso_time(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ProspectiveStateError(f"{field} must be an ISO timestamp")
    parsed = _parse_time(value, field)
    return parsed.isoformat()


def _optional_time(value: Any, field: str) -> str | None:
    return None if value is None else _iso_time(value, field)


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ProspectiveStateError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ProspectiveStateError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ProspectiveStateError(f"{field} must include a timezone")
    return parsed
