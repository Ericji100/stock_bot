from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .strategy_catalog import STRATEGY_METHODS


CONTEXT_VERSION = 1
LEG_LIMIT = 12

ENGINE_MODES = {"TAIJI_ORDERED", "YIZHI_MOMENTUM", "UNORDERED", "RESETTING", "UNDEFINED"}
ORDER_STATES = {"ORDERED", "UNORDERED", "TRANSITION", "UNDEFINED"}
LEG_SEQUENCES = {"ANCHOR_1", "CORRECTION_2", "COPY_3", "CORRECTION_4", "COPY_5", "POST_5"}
LEG_ROLES = {"ANCHOR", "CORRECTION", "COPY", "POST_TREND"}
LEG_STATUSES = {"FORMING", "CONFIRMED", "COMPLETED", "INVALIDATED"}
TERMINAL_LEG_STATUSES = {"COMPLETED", "INVALIDATED"}
QUALITY = {"STRONG", "ACCEPTABLE", "WEAK", "FAILED", "NOT_APPLICABLE", "UNKNOWN"}
RELATIONS = {"STRONGER", "SIMILAR", "WEAKER", "NOT_APPLICABLE", "UNKNOWN"}
ANCHOR_TIME_STATUS = {"FRESH", "DECAYING", "STALE", "UNDEFINED"}
PREVIOUS_CONTEXT_ALIGNMENT = {"ALIGNED", "OPPOSED", "NEUTRAL", "UNDEFINED"}

MOMENTUM_STAGES = {
    "NONE",
    "CENTRIFUGAL_FORMING",
    "CENTRIFUGAL_CONFIRMED",
    "DRAGON_EARLY",
    "DRAGON_MIDDLE",
    "DRAGON_LATE",
    "LIFE_DEATH_GATE_FORMING",
    "LIFE_DEATH_GATE_ARMED",
    "EXHAUSTION_WARNING",
    "FAILED",
}
MOMENTUM_LOCATIONS = {"SESSION_EXTERNAL", "SESSION_INTERNAL", "BOUNDARY", "UNDEFINED"}
MOMENTUM_QUALITY = {"STRONG", "ACCEPTABLE", "CAUTION", "UNQUALIFIED", "UNKNOWN"}
DRAGON_GRADES = {"GOLD", "K_GOLD", "EARTH", "CROOKED", "NOT_APPLICABLE", "UNKNOWN"}
DISTANCE_PHASES = {"EARLY", "MIDDLE", "LATE", "UNDEFINED"}
BAR_EXPANSION = {"EXPANDING", "STABLE", "CONTRACTING", "UNSTABLE", "UNKNOWN"}
PIVOT_PRESSURE = {"NONE", "MICRO", "STRUCTURAL", "UNKNOWN"}
GATE_BALANCE = {
    "CONTINUATION_STRONGER",
    "REVERSAL_STRONGER",
    "BALANCED",
    "NOT_APPLICABLE",
    "UNKNOWN",
}
PATTERN_TYPES = STRATEGY_METHODS
THESIS_BIASES = {"BULLISH", "BEARISH", "NEUTRAL", "CONDITIONAL", "UNDEFINED"}


class CClassStateError(ValueError):
    pass


def empty_cclass_context(*, as_of: str) -> dict[str, Any]:
    return {
        "version": CONTEXT_VERSION,
        "as_of": _iso_time(as_of, "as_of"),
        "engine_mode": "UNDEFINED",
        "engine_changed_at": None,
        "order_state": "UNDEFINED",
        "order_reason": "尚無足夠已收盤結構判斷市場節奏。",
        "taiji_context": {
            "dynasty_id": None,
            "anchor_id": None,
            "active_leg_id": None,
            "legs": [],
            "anchor_time_status": "UNDEFINED",
            "previous_context_alignment": "UNDEFINED",
            "assessment_reason": "尚未建立太極定錨與複製／修正序列。",
            "assessment_changed_at": None,
        },
        "momentum_context": {
            "episode_id": None,
            "stage": "NONE",
            "direction": None,
            "first_seen_at": None,
            "stage_changed_at": None,
            "location": "UNDEFINED",
            "quality": "UNKNOWN",
            "dragon_grade": "NOT_APPLICABLE",
            "distance_phase": "UNDEFINED",
            "bar_expansion": "UNKNOWN",
            "pivot_pressure": "UNKNOWN",
            "gate_balance": "NOT_APPLICABLE",
            "mapped_pattern": "NONE",
            "failure_reason": "目前沒有一之戰法動能事件。",
        },
        "thesis_context": {
            "bias": "UNDEFINED",
            "current_reason": "尚無可驗證的工作看法。",
            "hold_condition": "等待有效定錨與市場節奏成立。",
            "downgrade_condition": "資料不足，尚無可降級條件。",
            "neutralize_condition": "資料不足，尚無可中立化條件。",
            "reverse_condition": "必須先有反向防線破壞、反向錨與延續確認。",
            "changed_at": None,
        },
    }


def validate_cclass_context(value: Any, *, as_of: str) -> dict[str, Any]:
    keys = {
        "version",
        "as_of",
        "engine_mode",
        "engine_changed_at",
        "order_state",
        "order_reason",
        "taiji_context",
        "momentum_context",
        "thesis_context",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise CClassStateError("cclass_context has invalid fields")
    if value.get("version") != CONTEXT_VERSION:
        raise CClassStateError("cclass_context.version is invalid")
    authoritative = _iso_time(as_of, "as_of")
    if _iso_time(value.get("as_of"), "cclass_context.as_of") != authoritative:
        raise CClassStateError("cclass_context.as_of must match market_structure_state.as_of")
    ceiling = _parse_time(authoritative, "as_of")
    engine_mode = _enum(value.get("engine_mode"), ENGINE_MODES, "cclass_context.engine_mode")
    engine_changed_at = _optional_time(value.get("engine_changed_at"), "cclass_context.engine_changed_at")
    if engine_mode == "UNDEFINED":
        if engine_changed_at is not None:
            raise CClassStateError("undefined engine mode cannot have engine_changed_at")
    elif engine_changed_at is None or _parse_time(engine_changed_at, "engine_changed_at") > ceiling:
        raise CClassStateError("active engine mode requires a causal engine_changed_at")

    taiji = _taiji_context(value.get("taiji_context"), authoritative)
    momentum = _momentum_context(value.get("momentum_context"), authoritative)
    thesis = _thesis_context(value.get("thesis_context"), authoritative)
    if engine_mode == "TAIJI_ORDERED" and taiji["active_leg_id"] is None:
        raise CClassStateError("TAIJI_ORDERED requires an active causal Taiji leg")
    if engine_mode == "YIZHI_MOMENTUM" and momentum["stage"] in {"NONE", "FAILED"}:
        raise CClassStateError("YIZHI_MOMENTUM requires an active momentum stage")
    if engine_mode == "UNORDERED" and value.get("order_state") != "UNORDERED":
        raise CClassStateError("UNORDERED engine mode requires order_state=UNORDERED")

    return {
        "version": CONTEXT_VERSION,
        "as_of": authoritative,
        "engine_mode": engine_mode,
        "engine_changed_at": engine_changed_at,
        "order_state": _enum(value.get("order_state"), ORDER_STATES, "cclass_context.order_state"),
        "order_reason": _text(value.get("order_reason"), "cclass_context.order_reason", 240),
        "taiji_context": taiji,
        "momentum_context": momentum,
        "thesis_context": thesis,
    }


def validate_cclass_transition(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    prior_as_of: str,
    current_as_of: str,
    allow_taiji_lineage_replacement: bool = False,
) -> None:
    before = validate_cclass_context(previous, as_of=prior_as_of)
    after = validate_cclass_context(current, as_of=current_as_of)
    prior_time = _parse_time(prior_as_of, "prior_as_of")

    _validate_changed_at(
        changed=(before["engine_mode"], before["order_state"]) != (after["engine_mode"], after["order_state"]),
        before=before["engine_changed_at"],
        after=after["engine_changed_at"],
        prior_time=prior_time,
        field="engine_changed_at",
    )
    _validate_taiji_transition(
        before["taiji_context"],
        after["taiji_context"],
        prior_time,
        allow_lineage_replacement=allow_taiji_lineage_replacement,
    )
    _validate_momentum_transition(before["momentum_context"], after["momentum_context"], prior_time)
    _validate_thesis_transition(before["thesis_context"], after["thesis_context"], prior_time)


def cclass_notification_reasons(previous: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    before = previous
    after = current
    reasons: list[str] = []
    if (before["engine_mode"], before["order_state"]) != (after["engine_mode"], after["order_state"]):
        reasons.append("太極／一之主模式或市場秩序改變")
    before_taiji = before["taiji_context"]
    after_taiji = after["taiji_context"]
    if before_taiji["active_leg_id"] != after_taiji["active_leg_id"]:
        reasons.append("太極複製／修正階段改變")
    elif before_taiji["active_leg_id"] is not None:
        old = {item["leg_id"]: item for item in before_taiji["legs"]}.get(before_taiji["active_leg_id"])
        new = {item["leg_id"]: item for item in after_taiji["legs"]}.get(after_taiji["active_leg_id"])
        if old and new and any(
            old[key] != new[key]
            for key in ("status", "copy_quality", "correction_quality", "amplitude_relation", "duration_relation", "slope_relation", "cleanliness_relation", "destructive_relation")
        ):
            reasons.append("太極複製／修正品質改變")
    if before_taiji["anchor_time_status"] != after_taiji["anchor_time_status"]:
        reasons.append("定錨時間效力改變")
    before_momentum = before["momentum_context"]
    after_momentum = after["momentum_context"]
    if any(
        before_momentum[key] != after_momentum[key]
        for key in ("episode_id", "stage", "quality", "dragon_grade", "distance_phase", "mapped_pattern")
    ):
        reasons.append("離心力／一條龍／生死門階段改變")
    if before["thesis_context"]["bias"] != after["thesis_context"]["bias"]:
        reasons.append("工作看法改變")
    return reasons


def _taiji_context(value: Any, as_of: str) -> dict[str, Any]:
    keys = {
        "dynasty_id",
        "anchor_id",
        "active_leg_id",
        "legs",
        "anchor_time_status",
        "previous_context_alignment",
        "assessment_reason",
        "assessment_changed_at",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise CClassStateError("taiji_context has invalid fields")
    legs_value = value.get("legs")
    if not isinstance(legs_value, list) or len(legs_value) > LEG_LIMIT:
        raise CClassStateError(f"taiji_context.legs must contain at most {LEG_LIMIT} items")
    legs = [_leg(item, as_of=as_of) for item in legs_value]
    ids = [item["leg_id"] for item in legs]
    if len(ids) != len(set(ids)):
        raise CClassStateError("Taiji leg ids must be unique")
    by_id = {item["leg_id"]: item for item in legs}
    for item in legs:
        parent = item["parent_leg_id"]
        if parent is not None and parent not in by_id:
            raise CClassStateError("Taiji parent_leg_id must reference a retained leg")
    active_leg_id = _optional_text(value.get("active_leg_id"), "taiji_context.active_leg_id", 120)
    dynasty_id = _optional_text(value.get("dynasty_id"), "taiji_context.dynasty_id", 120)
    anchor_id = _optional_text(value.get("anchor_id"), "taiji_context.anchor_id", 120)
    if active_leg_id is None:
        if legs or dynasty_id is not None or anchor_id is not None or value.get("assessment_changed_at") is not None:
            raise CClassStateError("inactive Taiji context must not retain an active dynasty or legs")
    else:
        active = by_id.get(active_leg_id)
        if active is None or active["status"] in TERMINAL_LEG_STATUSES:
            raise CClassStateError("taiji_context.active_leg_id is invalid")
        if dynasty_id is None or anchor_id is None:
            raise CClassStateError("active Taiji context requires dynasty_id and anchor_id")
    assessment_changed_at = _optional_time(value.get("assessment_changed_at"), "taiji_context.assessment_changed_at")
    if active_leg_id is not None and (
        assessment_changed_at is None
        or _parse_time(assessment_changed_at, "taiji_context.assessment_changed_at") > _parse_time(as_of, "as_of")
    ):
        raise CClassStateError("active Taiji context requires a causal assessment_changed_at")
    return {
        "dynasty_id": dynasty_id,
        "anchor_id": anchor_id,
        "active_leg_id": active_leg_id,
        "legs": legs,
        "anchor_time_status": _enum(value.get("anchor_time_status"), ANCHOR_TIME_STATUS, "taiji_context.anchor_time_status"),
        "previous_context_alignment": _enum(
            value.get("previous_context_alignment"),
            PREVIOUS_CONTEXT_ALIGNMENT,
            "taiji_context.previous_context_alignment",
        ),
        "assessment_reason": _text(value.get("assessment_reason"), "taiji_context.assessment_reason", 240),
        "assessment_changed_at": assessment_changed_at,
    }


def _leg(value: Any, *, as_of: str) -> dict[str, Any]:
    keys = {
        "leg_id",
        "sequence",
        "role",
        "direction",
        "status",
        "start_bar_time",
        "extreme_bar_time",
        "first_seen_at",
        "confirmed_at",
        "terminal_at",
        "parent_leg_id",
        "copy_quality",
        "correction_quality",
        "amplitude_relation",
        "duration_relation",
        "slope_relation",
        "cleanliness_relation",
        "destructive_relation",
        "notes",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise CClassStateError("Taiji leg has invalid fields")
    sequence = _enum(value.get("sequence"), LEG_SEQUENCES, "Taiji leg.sequence")
    role = _enum(value.get("role"), LEG_ROLES, "Taiji leg.role")
    expected_role = {
        "ANCHOR_1": "ANCHOR",
        "CORRECTION_2": "CORRECTION",
        "COPY_3": "COPY",
        "CORRECTION_4": "CORRECTION",
        "COPY_5": "COPY",
        "POST_5": "POST_TREND",
    }[sequence]
    if role != expected_role:
        raise CClassStateError("Taiji leg role does not match its sequence")
    status = _enum(value.get("status"), LEG_STATUSES, "Taiji leg.status")
    start = _iso_time(value.get("start_bar_time"), "Taiji leg.start_bar_time")
    extreme = _iso_time(value.get("extreme_bar_time"), "Taiji leg.extreme_bar_time")
    first_seen = _iso_time(value.get("first_seen_at"), "Taiji leg.first_seen_at")
    confirmed = _optional_time(value.get("confirmed_at"), "Taiji leg.confirmed_at")
    terminal = _optional_time(value.get("terminal_at"), "Taiji leg.terminal_at")
    ceiling = _parse_time(as_of, "as_of")
    timestamps = [start, extreme, first_seen, *[item for item in (confirmed, terminal) if item]]
    if any(_parse_time(item, "Taiji leg timestamp") > ceiling for item in timestamps):
        raise CClassStateError("Taiji leg contains a future timestamp")
    if _parse_time(extreme, "extreme_bar_time") < _parse_time(start, "start_bar_time"):
        raise CClassStateError("Taiji leg extreme cannot predate its start")
    if status == "FORMING" and (confirmed is not None or terminal is not None):
        raise CClassStateError("forming Taiji leg cannot have confirmation or terminal time")
    if status == "CONFIRMED" and (confirmed is None or terminal is not None):
        raise CClassStateError("confirmed Taiji leg requires confirmed_at and no terminal_at")
    if status in TERMINAL_LEG_STATUSES and terminal is None:
        raise CClassStateError("terminal Taiji leg requires terminal_at")
    copy_quality = _enum(value.get("copy_quality"), QUALITY, "Taiji leg.copy_quality")
    correction_quality = _enum(value.get("correction_quality"), QUALITY, "Taiji leg.correction_quality")
    if role == "COPY" and copy_quality == "NOT_APPLICABLE":
        raise CClassStateError("copy leg must assess copy quality")
    if role == "CORRECTION" and correction_quality == "NOT_APPLICABLE":
        raise CClassStateError("correction leg must assess correction quality")
    notes = value.get("notes")
    if not isinstance(notes, list) or not 1 <= len(notes) <= 3:
        raise CClassStateError("Taiji leg.notes must contain one to three items")
    return {
        "leg_id": _text(value.get("leg_id"), "Taiji leg.leg_id", 120),
        "sequence": sequence,
        "role": role,
        "direction": _enum(value.get("direction"), {"BULL", "BEAR"}, "Taiji leg.direction"),
        "status": status,
        "start_bar_time": start,
        "extreme_bar_time": extreme,
        "first_seen_at": first_seen,
        "confirmed_at": confirmed,
        "terminal_at": terminal,
        "parent_leg_id": _optional_text(value.get("parent_leg_id"), "Taiji leg.parent_leg_id", 120),
        "copy_quality": copy_quality,
        "correction_quality": correction_quality,
        "amplitude_relation": _enum(value.get("amplitude_relation"), RELATIONS, "Taiji leg.amplitude_relation"),
        "duration_relation": _enum(value.get("duration_relation"), RELATIONS, "Taiji leg.duration_relation"),
        "slope_relation": _enum(value.get("slope_relation"), RELATIONS, "Taiji leg.slope_relation"),
        "cleanliness_relation": _enum(value.get("cleanliness_relation"), RELATIONS, "Taiji leg.cleanliness_relation"),
        "destructive_relation": _enum(value.get("destructive_relation"), RELATIONS, "Taiji leg.destructive_relation"),
        "notes": [_text(item, "Taiji leg.notes item", 180) for item in notes],
    }


def _momentum_context(value: Any, as_of: str) -> dict[str, Any]:
    keys = {
        "episode_id",
        "stage",
        "direction",
        "first_seen_at",
        "stage_changed_at",
        "location",
        "quality",
        "dragon_grade",
        "distance_phase",
        "bar_expansion",
        "pivot_pressure",
        "gate_balance",
        "mapped_pattern",
        "failure_reason",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise CClassStateError("momentum_context has invalid fields")
    stage = _enum(value.get("stage"), MOMENTUM_STAGES, "momentum_context.stage")
    episode_id = _optional_text(value.get("episode_id"), "momentum_context.episode_id", 120)
    direction = value.get("direction")
    first_seen = _optional_time(value.get("first_seen_at"), "momentum_context.first_seen_at")
    changed = _optional_time(value.get("stage_changed_at"), "momentum_context.stage_changed_at")
    if stage == "NONE":
        if any(item is not None for item in (episode_id, direction, first_seen, changed)):
            raise CClassStateError("inactive momentum context must use null identity and timestamps")
    else:
        episode_id = _text(episode_id, "momentum_context.episode_id", 120)
        direction = _enum(direction, {"BULL", "BEAR"}, "momentum_context.direction")
        if first_seen is None or changed is None:
            raise CClassStateError("active momentum context requires causal timestamps")
        ceiling = _parse_time(as_of, "as_of")
        if _parse_time(first_seen, "first_seen_at") > _parse_time(changed, "stage_changed_at") or _parse_time(changed, "stage_changed_at") > ceiling:
            raise CClassStateError("momentum timestamps are not causal")
    dragon_grade = _enum(value.get("dragon_grade"), DRAGON_GRADES, "momentum_context.dragon_grade")
    if not stage.startswith("DRAGON") and dragon_grade not in {"NOT_APPLICABLE", "UNKNOWN"}:
        # dragon_grade has no trading meaning outside an active dragon stage.
        # A model may retain the last observed grade while moving the episode to
        # exhaustion/failed.  Normalize that irrelevant carry-over instead of
        # discarding an otherwise valid market analysis.
        dragon_grade = "NOT_APPLICABLE"
    gate_balance = _enum(value.get("gate_balance"), GATE_BALANCE, "momentum_context.gate_balance")
    if not stage.startswith("LIFE_DEATH_GATE") and gate_balance not in {"NOT_APPLICABLE", "UNKNOWN"}:
        raise CClassStateError("gate balance is only valid during a life-death-gate stage")
    return {
        "episode_id": episode_id,
        "stage": stage,
        "direction": direction,
        "first_seen_at": first_seen,
        "stage_changed_at": changed,
        "location": _enum(value.get("location"), MOMENTUM_LOCATIONS, "momentum_context.location"),
        "quality": _enum(value.get("quality"), MOMENTUM_QUALITY, "momentum_context.quality"),
        "dragon_grade": dragon_grade,
        "distance_phase": _enum(value.get("distance_phase"), DISTANCE_PHASES, "momentum_context.distance_phase"),
        "bar_expansion": _enum(value.get("bar_expansion"), BAR_EXPANSION, "momentum_context.bar_expansion"),
        "pivot_pressure": _enum(value.get("pivot_pressure"), PIVOT_PRESSURE, "momentum_context.pivot_pressure"),
        "gate_balance": gate_balance,
        "mapped_pattern": _enum(value.get("mapped_pattern"), PATTERN_TYPES, "momentum_context.mapped_pattern"),
        "failure_reason": _text(value.get("failure_reason"), "momentum_context.failure_reason", 240),
    }


def _thesis_context(value: Any, as_of: str) -> dict[str, Any]:
    keys = {
        "bias",
        "current_reason",
        "hold_condition",
        "downgrade_condition",
        "neutralize_condition",
        "reverse_condition",
        "changed_at",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise CClassStateError("thesis_context has invalid fields")
    bias = _enum(value.get("bias"), THESIS_BIASES, "thesis_context.bias")
    changed = _optional_time(value.get("changed_at"), "thesis_context.changed_at")
    if bias == "UNDEFINED":
        if changed is not None:
            raise CClassStateError("undefined thesis cannot have changed_at")
    elif changed is None or _parse_time(changed, "thesis_context.changed_at") > _parse_time(as_of, "as_of"):
        raise CClassStateError("active thesis requires a causal changed_at")
    return {
        "bias": bias,
        "current_reason": _text(value.get("current_reason"), "thesis_context.current_reason", 240),
        "hold_condition": _text(value.get("hold_condition"), "thesis_context.hold_condition", 240),
        "downgrade_condition": _text(value.get("downgrade_condition"), "thesis_context.downgrade_condition", 240),
        "neutralize_condition": _text(value.get("neutralize_condition"), "thesis_context.neutralize_condition", 240),
        "reverse_condition": _text(value.get("reverse_condition"), "thesis_context.reverse_condition", 240),
        "changed_at": changed,
    }


def _validate_taiji_transition(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    prior_time: datetime,
    *,
    allow_lineage_replacement: bool = False,
) -> None:
    old = {item["leg_id"]: item for item in before["legs"]}
    new = {item["leg_id"]: item for item in after["legs"]}
    for leg_id, prior in old.items():
        current = new.get(leg_id)
        if current is None:
            if prior["status"] not in TERMINAL_LEG_STATUSES and not allow_lineage_replacement:
                raise CClassStateError("active or confirmed Taiji legs cannot disappear")
            continue
        for key in ("leg_id", "sequence", "role", "direction", "start_bar_time", "first_seen_at", "parent_leg_id"):
            if prior[key] != current[key]:
                raise CClassStateError(f"Taiji leg changed immutable field {key}")
        allowed = {
            "FORMING": {"FORMING", "CONFIRMED", "INVALIDATED"},
            "CONFIRMED": {"CONFIRMED", "COMPLETED", "INVALIDATED"},
            "COMPLETED": {"COMPLETED"},
            "INVALIDATED": {"INVALIDATED"},
        }
        if current["status"] not in allowed[prior["status"]]:
            raise CClassStateError("Taiji leg has an invalid status transition")
    for leg_id in set(new) - set(old):
        if _parse_time(new[leg_id]["first_seen_at"], "new Taiji leg first_seen_at") <= prior_time:
            raise CClassStateError("new Taiji legs cannot backfill first_seen_at")
    if before["active_leg_id"] and before["active_leg_id"] != after["active_leg_id"]:
        prior_active = new.get(before["active_leg_id"])
        if (
            (prior_active is None or prior_active["status"] not in TERMINAL_LEG_STATUSES)
            and not allow_lineage_replacement
        ):
            raise CClassStateError("active Taiji leg must terminate before switching")
    before_active = old.get(before["active_leg_id"]) if before["active_leg_id"] else None
    after_active = new.get(after["active_leg_id"]) if after["active_leg_id"] else None
    quality_fields = (
        "status",
        "copy_quality",
        "correction_quality",
        "amplitude_relation",
        "duration_relation",
        "slope_relation",
        "cleanliness_relation",
        "destructive_relation",
    )
    changed = (
        before["active_leg_id"] != after["active_leg_id"]
        or before["anchor_time_status"] != after["anchor_time_status"]
        or before["previous_context_alignment"] != after["previous_context_alignment"]
        or (
            before_active is not None
            and after_active is not None
            and any(before_active[key] != after_active[key] for key in quality_fields)
        )
    )
    _validate_changed_at(
        changed=changed,
        before=before["assessment_changed_at"],
        after=after["assessment_changed_at"],
        prior_time=prior_time,
        field="taiji_context.assessment_changed_at",
    )


def _validate_momentum_transition(before: Mapping[str, Any], after: Mapping[str, Any], prior_time: datetime) -> None:
    if before["episode_id"] is not None and before["episode_id"] == after["episode_id"]:
        for key in ("episode_id", "direction", "first_seen_at"):
            if before[key] != after[key]:
                raise CClassStateError(f"momentum episode changed immutable field {key}")
    if before["episode_id"] != after["episode_id"]:
        if before["stage"] not in {"NONE", "FAILED", "EXHAUSTION_WARNING"}:
            raise CClassStateError("momentum episode cannot switch before prior episode terminates")
        if after["episode_id"] is not None and _parse_time(after["first_seen_at"], "new momentum first_seen_at") <= prior_time:
            raise CClassStateError("new momentum episode cannot backfill first_seen_at")
    changed = any(
        before[key] != after[key]
        for key in ("stage", "quality", "dragon_grade", "distance_phase", "location", "mapped_pattern")
    )
    _validate_changed_at(
        changed=changed,
        before=before["stage_changed_at"],
        after=after["stage_changed_at"],
        prior_time=prior_time,
        field="momentum_context.stage_changed_at",
    )


def _validate_thesis_transition(before: Mapping[str, Any], after: Mapping[str, Any], prior_time: datetime) -> None:
    _validate_changed_at(
        changed=before["bias"] != after["bias"],
        before=before["changed_at"],
        after=after["changed_at"],
        prior_time=prior_time,
        field="thesis_context.changed_at",
    )


def _validate_changed_at(
    *,
    changed: bool,
    before: str | None,
    after: str | None,
    prior_time: datetime,
    field: str,
) -> None:
    if changed:
        # Empty/undefined target states intentionally use a null timestamp.
        if after is None:
            return
        if _parse_time(after, field) <= prior_time:
            raise CClassStateError(f"{field} must advance when its state changes")
    elif before != after:
        raise CClassStateError(f"{field} changed without a state change")


def _enum(value: Any, allowed: set[str], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise CClassStateError(f"{field} is invalid")
    return value


def _text(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise CClassStateError(f"{field} must be text")
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > max_length:
        raise CClassStateError(f"{field} has invalid length")
    return cleaned


def _optional_text(value: Any, field: str, max_length: int) -> str | None:
    return None if value is None else _text(value, field, max_length)


def _iso_time(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise CClassStateError(f"{field} must be an ISO timestamp")
    parsed = _parse_time(value, field)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CClassStateError(f"{field} must include a timezone")
    return value


def _optional_time(value: Any, field: str) -> str | None:
    return None if value is None else _iso_time(value, field)


def _parse_time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise CClassStateError(f"{field} is not a valid ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CClassStateError(f"{field} must include a timezone")
    return parsed
