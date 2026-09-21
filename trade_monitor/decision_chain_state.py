from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .strategy_catalog import STRATEGY_METHODS


CONTEXT_VERSION = 1

PROCESS_STAGES = {
    "UNDEFINED",
    "SESSION_OPEN_PENDING",
    "OPENING_EVIDENCE",
    "FIRST_ENDPOINT_SAMPLE",
    "STRUCTURE_BUILDING",
    "SETUP_EVALUATION",
    "LATE_OR_RESETTING",
}
GAP_SOURCES = {"VERIFIED_CASH", "FUTURES_PROXY", "UNAVAILABLE"}
DIRECTIONS = {"BULL", "BEAR", "FLAT", "UNDEFINED"}
GAP_SIZES = {"SMALL", "MODERATE", "LARGE", "EXHAUSTED", "UNDEFINED"}
OPENING_RELATIONS = {"SAME", "OPPOSITE", "MIXED", "UNDEFINED"}
OPENING_QUALITY = {"CLEAN", "MIXED", "NOISY", "OVERHEATED", "UNAVAILABLE"}
ENDPOINT_SIDES = {"DH", "DL", "NONE"}
ENDPOINT_STYLES = {"PENDING", "TRUE_LIKE", "FALSE_LIKE", "MIXED", "UNAVAILABLE"}
ANALYSIS_LENSES = {
    "UNDEFINED",
    "OPENING_EVIDENCE_ONLY",
    "TAIJI_PRIMARY",
    "QUADRANT_PRIMARY",
    "COMBINED_CONFIRMATION",
    "YIZHI_OVERRIDE",
}
DNA_STATES = {"CONSISTENT", "CHANGING", "BROKEN", "UNAVAILABLE"}
CONFLUENCES = {"SAME_QUADRANT", "SMALL_Q1", "COPY_CORRECTION"}
EVIDENCE_LEVELS = {"HIGH", "MEDIUM", "LOW", "UNAVAILABLE"}
COURSE_GRADES = {"A_CANDIDATE", "B_CANDIDATE", "C_CANDIDATE", "OBSERVE", "UNDEFINED"}
PATTERN_TYPES = STRATEGY_METHODS
RECOVERY_POLICIES = {"NOT_APPLICABLE", "EXIT_ON_FIRST_VALID_RECOVERY"}


class DecisionChainStateError(ValueError):
    pass


def empty_decision_chain_context(*, as_of: str) -> dict[str, Any]:
    return {
        "version": CONTEXT_VERSION,
        "as_of": _iso_time(as_of, "as_of"),
        "process_stage": "UNDEFINED",
        "stage_changed_at": None,
        "opening_context": {
            "cash_gap_source": "UNAVAILABLE",
            "gap_direction": "UNDEFINED",
            "gap_size": "UNDEFINED",
            "previous_cash_close": None,
            "cash_open": None,
            "gap_observed_at": None,
            "opening_direction_relation": "UNDEFINED",
            "pre_cash_open_quality": "UNAVAILABLE",
            "first_endpoint_side": "NONE",
            "first_endpoint_style": "UNAVAILABLE",
            "endpoint_first_seen_at": None,
            "endpoint_confirmed_at": None,
            "evidence_reason": "尚無可靠現貨跳空、開盤品質或第一次端點突破證據。",
        },
        "structure_context": {
            "confirmed_leg_count": 0,
            "analysis_lens": "UNDEFINED",
            "lens_changed_at": None,
            "family_dna": "UNAVAILABLE",
            "confluences": [],
            "structure_reason": "尚未形成可比較的兩腳結構。",
        },
        "opportunity_context": {
            "mapped_pattern": "NONE",
            "probability_evidence": "UNAVAILABLE",
            "payoff_evidence": "UNAVAILABLE",
            "course_grade": "UNDEFINED",
            "grade_reason": "尚無原四型態候選，不能評定 X 機會品質。",
            "expected_behavior": "等待原四型態、結構停損與收盤觸發成立。",
            "max_wait_bars": None,
            "behavior_invalidation": "資料不足，尚無可執行的時間／動機失效條件。",
            "recovery_round_policy": "NOT_APPLICABLE",
        },
        "assessment_changed_at": None,
    }


def validate_decision_chain_context(value: Any, *, as_of: str) -> dict[str, Any]:
    keys = {
        "version",
        "as_of",
        "process_stage",
        "stage_changed_at",
        "opening_context",
        "structure_context",
        "opportunity_context",
        "assessment_changed_at",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise DecisionChainStateError("decision_chain_context has invalid fields")
    if value.get("version") != CONTEXT_VERSION:
        raise DecisionChainStateError("decision_chain_context.version is invalid")
    authoritative = _iso_time(as_of, "as_of")
    if _iso_time(value.get("as_of"), "decision_chain_context.as_of") != authoritative:
        raise DecisionChainStateError("decision_chain_context.as_of must match market_structure_state.as_of")
    ceiling = _parse_time(authoritative, "as_of")
    process_stage = _enum(value.get("process_stage"), PROCESS_STAGES, "process_stage")
    stage_changed_at = _optional_time(value.get("stage_changed_at"), "stage_changed_at")
    assessment_changed_at = _optional_time(value.get("assessment_changed_at"), "assessment_changed_at")
    if process_stage == "UNDEFINED":
        if stage_changed_at is not None or assessment_changed_at is not None:
            raise DecisionChainStateError("undefined decision chain cannot have change timestamps")
    else:
        for field, item in (("stage_changed_at", stage_changed_at), ("assessment_changed_at", assessment_changed_at)):
            if item is None or _parse_time(item, field) > ceiling:
                raise DecisionChainStateError("active decision chain requires causal change timestamps")

    opening = _opening_context(value.get("opening_context"), authoritative)
    structure = _structure_context(value.get("structure_context"), authoritative)
    opportunity = _opportunity_context(value.get("opportunity_context"), process_stage)
    if structure["analysis_lens"] == "OPENING_EVIDENCE_ONLY" and process_stage not in {
        "SESSION_OPEN_PENDING",
        "OPENING_EVIDENCE",
        "FIRST_ENDPOINT_SAMPLE",
        "STRUCTURE_BUILDING",
    }:
        # Once the process has advanced beyond opening/structure building, an
        # opening-only lens is stale bookkeeping rather than a market opinion.
        # Clear it deterministically; transition timestamps are normalized by
        # the outer market-structure transition validator.
        structure["analysis_lens"] = "UNDEFINED"
        structure["lens_changed_at"] = None
    if opportunity["course_grade"] in {"A_CANDIDATE", "B_CANDIDATE", "C_CANDIDATE"}:
        if process_stage != "SETUP_EVALUATION":
            raise DecisionChainStateError("tradable course grades require SETUP_EVALUATION")
        if opportunity["mapped_pattern"] == "NONE":
            raise DecisionChainStateError("a tradable course grade requires a selected strategy method")

    return {
        "version": CONTEXT_VERSION,
        "as_of": authoritative,
        "process_stage": process_stage,
        "stage_changed_at": stage_changed_at,
        "opening_context": opening,
        "structure_context": structure,
        "opportunity_context": opportunity,
        "assessment_changed_at": assessment_changed_at,
    }


def validate_decision_chain_transition(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    prior_as_of: str,
    current_as_of: str,
) -> None:
    before = validate_decision_chain_context(previous, as_of=prior_as_of)
    after = validate_decision_chain_context(current, as_of=current_as_of)
    prior_time = _parse_time(prior_as_of, "prior_as_of")
    allowed = {
        "UNDEFINED": PROCESS_STAGES,
        "SESSION_OPEN_PENDING": {"SESSION_OPEN_PENDING", "OPENING_EVIDENCE"},
        "OPENING_EVIDENCE": {"OPENING_EVIDENCE", "FIRST_ENDPOINT_SAMPLE", "STRUCTURE_BUILDING"},
        "FIRST_ENDPOINT_SAMPLE": {"FIRST_ENDPOINT_SAMPLE", "STRUCTURE_BUILDING"},
        "STRUCTURE_BUILDING": {"STRUCTURE_BUILDING", "SETUP_EVALUATION", "LATE_OR_RESETTING"},
        "SETUP_EVALUATION": {"SETUP_EVALUATION", "STRUCTURE_BUILDING", "LATE_OR_RESETTING"},
        "LATE_OR_RESETTING": {"LATE_OR_RESETTING", "STRUCTURE_BUILDING", "SETUP_EVALUATION"},
    }
    if after["process_stage"] not in allowed[before["process_stage"]]:
        raise DecisionChainStateError("decision chain process stage moved through an invalid transition")
    _validate_changed_at(
        changed=before["process_stage"] != after["process_stage"],
        before=before["stage_changed_at"],
        after=after["stage_changed_at"],
        prior_time=prior_time,
        field="stage_changed_at",
    )

    old_open = before["opening_context"]
    new_open = after["opening_context"]
    if old_open["cash_gap_source"] != "UNAVAILABLE":
        for key in (
            "cash_gap_source",
            "gap_direction",
            "gap_size",
            "previous_cash_close",
            "cash_open",
            "gap_observed_at",
        ):
            if old_open[key] != new_open[key]:
                raise DecisionChainStateError(f"opening cash-gap evidence changed immutable field {key}")
    if old_open["first_endpoint_side"] != "NONE":
        for key in ("first_endpoint_side", "endpoint_first_seen_at"):
            if old_open[key] != new_open[key]:
                raise DecisionChainStateError(f"first endpoint sample changed immutable field {key}")
        if old_open["first_endpoint_style"] not in {"PENDING", "UNAVAILABLE"}:
            for key in ("first_endpoint_style", "endpoint_confirmed_at"):
                if old_open[key] != new_open[key]:
                    raise DecisionChainStateError("confirmed first endpoint sample cannot be rewritten")
    if old_open["first_endpoint_side"] == "NONE" and new_open["first_endpoint_side"] != "NONE":
        if _parse_time(new_open["endpoint_first_seen_at"], "endpoint_first_seen_at") <= prior_time:
            raise DecisionChainStateError("new first endpoint sample cannot backfill first_seen_at")

    _validate_changed_at(
        changed=(
            before["structure_context"]["analysis_lens"]
            != after["structure_context"]["analysis_lens"]
        ),
        before=before["structure_context"]["lens_changed_at"],
        after=after["structure_context"]["lens_changed_at"],
        prior_time=prior_time,
        field="lens_changed_at",
    )

    before_material = _material(before)
    after_material = _material(after)
    _validate_changed_at(
        changed=before_material != after_material,
        before=before["assessment_changed_at"],
        after=after["assessment_changed_at"],
        prior_time=prior_time,
        field="assessment_changed_at",
    )


def decision_chain_notification_reasons(previous: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    before = previous
    after = current
    reasons: list[str] = []
    if before["process_stage"] != after["process_stage"]:
        reasons.append("X 決策鏈階段改變")
    old_open = before["opening_context"]
    new_open = after["opening_context"]
    if any(
        old_open[key] != new_open[key]
        for key in ("cash_gap_source", "gap_direction", "gap_size", "opening_direction_relation", "pre_cash_open_quality")
    ):
        reasons.append("跳空／開盤品質證據改變")
    if any(
        old_open[key] != new_open[key]
        for key in ("first_endpoint_side", "first_endpoint_style", "endpoint_confirmed_at")
    ):
        reasons.append("第一次日內端點突破風格改變")
    old_structure = before["structure_context"]
    new_structure = after["structure_context"]
    if any(
        old_structure[key] != new_structure[key]
        for key in ("confirmed_leg_count", "analysis_lens", "family_dna", "confluences")
    ):
        reasons.append("主要判讀工具／家族 DNA／共振改變")
    old_opportunity = before["opportunity_context"]
    new_opportunity = after["opportunity_context"]
    if any(
        old_opportunity[key] != new_opportunity[key]
        for key in ("mapped_pattern", "probability_evidence", "payoff_evidence", "course_grade")
    ):
        reasons.append("X 機會品質改變")
    if any(
        old_opportunity[key] != new_opportunity[key]
        for key in ("expected_behavior", "max_wait_bars", "behavior_invalidation", "recovery_round_policy")
    ):
        reasons.append("進場後預期／時間動機失效條件改變")
    return reasons


def _opening_context(value: Any, as_of: str) -> dict[str, Any]:
    keys = {
        "cash_gap_source",
        "gap_direction",
        "gap_size",
        "previous_cash_close",
        "cash_open",
        "gap_observed_at",
        "opening_direction_relation",
        "pre_cash_open_quality",
        "first_endpoint_side",
        "first_endpoint_style",
        "endpoint_first_seen_at",
        "endpoint_confirmed_at",
        "evidence_reason",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise DecisionChainStateError("opening_context has invalid fields")
    source = _enum(value.get("cash_gap_source"), GAP_SOURCES, "opening_context.cash_gap_source")
    direction = _enum(value.get("gap_direction"), DIRECTIONS, "opening_context.gap_direction")
    size = _enum(value.get("gap_size"), GAP_SIZES, "opening_context.gap_size")
    previous_close = _optional_positive_number(value.get("previous_cash_close"), "previous_cash_close")
    cash_open = _optional_positive_number(value.get("cash_open"), "cash_open")
    observed = _optional_time(value.get("gap_observed_at"), "gap_observed_at")
    if source == "UNAVAILABLE":
        if direction != "UNDEFINED" or size != "UNDEFINED" or any(item is not None for item in (previous_close, cash_open, observed)):
            raise DecisionChainStateError("unavailable cash gap must not contain inferred values")
    else:
        if direction == "UNDEFINED" or size == "UNDEFINED" or observed is None:
            raise DecisionChainStateError("available gap evidence requires direction, normalized size, and observed_at")
        if source == "VERIFIED_CASH" and (previous_close is None or cash_open is None):
            raise DecisionChainStateError("verified cash gap requires both source prices")
        if _parse_time(observed, "gap_observed_at") > _parse_time(as_of, "as_of"):
            raise DecisionChainStateError("cash gap evidence cannot come from the future")

    side = _enum(value.get("first_endpoint_side"), ENDPOINT_SIDES, "first_endpoint_side")
    style = _enum(value.get("first_endpoint_style"), ENDPOINT_STYLES, "first_endpoint_style")
    first_seen = _optional_time(value.get("endpoint_first_seen_at"), "endpoint_first_seen_at")
    confirmed = _optional_time(value.get("endpoint_confirmed_at"), "endpoint_confirmed_at")
    if side == "NONE":
        if style not in {"PENDING", "UNAVAILABLE"} or first_seen is not None or confirmed is not None:
            raise DecisionChainStateError("inactive endpoint sample must not contain event timestamps")
    else:
        if first_seen is None:
            raise DecisionChainStateError("endpoint sample requires first_seen_at")
        if style in {"TRUE_LIKE", "FALSE_LIKE", "MIXED"} and confirmed is None:
            raise DecisionChainStateError("classified endpoint sample requires confirmed_at")
        if style == "PENDING" and confirmed is not None:
            raise DecisionChainStateError("pending endpoint sample cannot be confirmed")
        ceiling = _parse_time(as_of, "as_of")
        if _parse_time(first_seen, "endpoint_first_seen_at") > ceiling:
            raise DecisionChainStateError("endpoint sample cannot start in the future")
        if confirmed is not None and not (
            _parse_time(first_seen, "endpoint_first_seen_at")
            <= _parse_time(confirmed, "endpoint_confirmed_at")
            <= ceiling
        ):
            raise DecisionChainStateError("endpoint sample timestamps are not causal")
    return {
        "cash_gap_source": source,
        "gap_direction": direction,
        "gap_size": size,
        "previous_cash_close": previous_close,
        "cash_open": cash_open,
        "gap_observed_at": observed,
        "opening_direction_relation": _enum(
            value.get("opening_direction_relation"), OPENING_RELATIONS, "opening_direction_relation"
        ),
        "pre_cash_open_quality": _enum(
            value.get("pre_cash_open_quality"), OPENING_QUALITY, "pre_cash_open_quality"
        ),
        "first_endpoint_side": side,
        "first_endpoint_style": style,
        "endpoint_first_seen_at": first_seen,
        "endpoint_confirmed_at": confirmed,
        "evidence_reason": _text(value.get("evidence_reason"), "opening_context.evidence_reason", 300),
    }


def _structure_context(value: Any, as_of: str) -> dict[str, Any]:
    keys = {
        "confirmed_leg_count",
        "analysis_lens",
        "lens_changed_at",
        "family_dna",
        "confluences",
        "structure_reason",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise DecisionChainStateError("structure_context has invalid fields")
    leg_count = value.get("confirmed_leg_count")
    if not isinstance(leg_count, int) or isinstance(leg_count, bool) or not 0 <= leg_count <= 12:
        raise DecisionChainStateError("confirmed_leg_count must be an integer from 0 to 12")
    lens = _enum(value.get("analysis_lens"), ANALYSIS_LENSES, "analysis_lens")
    lens_changed = _optional_time(value.get("lens_changed_at"), "lens_changed_at")
    if lens != "UNDEFINED" and (
        lens_changed is None or _parse_time(lens_changed, "lens_changed_at") > _parse_time(as_of, "as_of")
    ):
        raise DecisionChainStateError("active analysis lens requires causal lens_changed_at")
    if lens == "UNDEFINED" and lens_changed is not None and (
        _parse_time(lens_changed, "lens_changed_at") > _parse_time(as_of, "as_of")
    ):
        raise DecisionChainStateError("analysis lens reset time cannot come from the future")
    if leg_count < 2 and lens in {"TAIJI_PRIMARY", "QUADRANT_PRIMARY", "COMBINED_CONFIRMATION"}:
        raise DecisionChainStateError("structural lens requires at least two confirmed legs")
    if leg_count < 3 and lens == "COMBINED_CONFIRMATION":
        raise DecisionChainStateError("combined lens requires at least three confirmed legs")
    confluences = value.get("confluences")
    if not isinstance(confluences, list) or len(confluences) > 3 or len(confluences) != len(set(confluences)):
        raise DecisionChainStateError("confluences must be a unique list with at most three items")
    if any(item not in CONFLUENCES for item in confluences):
        raise DecisionChainStateError("confluences contains an invalid item")
    return {
        "confirmed_leg_count": leg_count,
        "analysis_lens": lens,
        "lens_changed_at": lens_changed,
        "family_dna": _enum(value.get("family_dna"), DNA_STATES, "family_dna"),
        "confluences": confluences,
        "structure_reason": _text(value.get("structure_reason"), "structure_context.structure_reason", 300),
    }


def _opportunity_context(value: Any, process_stage: str) -> dict[str, Any]:
    keys = {
        "mapped_pattern",
        "probability_evidence",
        "payoff_evidence",
        "course_grade",
        "grade_reason",
        "expected_behavior",
        "max_wait_bars",
        "behavior_invalidation",
        "recovery_round_policy",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise DecisionChainStateError("opportunity_context has invalid fields")
    pattern = _enum(value.get("mapped_pattern"), PATTERN_TYPES, "mapped_pattern")
    probability = _enum(value.get("probability_evidence"), EVIDENCE_LEVELS, "probability_evidence")
    payoff = _enum(value.get("payoff_evidence"), EVIDENCE_LEVELS, "payoff_evidence")
    grade = _enum(value.get("course_grade"), COURSE_GRADES, "course_grade")
    max_wait = value.get("max_wait_bars")
    if max_wait is not None and (
        not isinstance(max_wait, int) or isinstance(max_wait, bool) or not 1 <= max_wait <= 10
    ):
        raise DecisionChainStateError("max_wait_bars must be null or an integer from 1 to 10")
    expected = {
        "A_CANDIDATE": ("HIGH", "HIGH"),
        "B_CANDIDATE": ("HIGH", "MEDIUM"),
        "C_CANDIDATE": ("MEDIUM", "HIGH"),
    }
    if grade in expected and (probability, payoff) != expected[grade]:
        raise DecisionChainStateError("course grade does not match probability/payoff evidence")
    if grade in expected and max_wait is None:
        raise DecisionChainStateError("A/B/C candidates require an explicit max_wait_bars")
    if grade == "UNDEFINED" and pattern != "NONE":
        raise DecisionChainStateError("mapped setup requires at least an OBSERVE course grade")
    recovery = _enum(value.get("recovery_round_policy"), RECOVERY_POLICIES, "recovery_round_policy")
    if recovery == "EXIT_ON_FIRST_VALID_RECOVERY" and grade != "A_CANDIDATE":
        raise DecisionChainStateError("recovery-round policy is limited to risk-capped A candidates")
    if grade in expected and process_stage != "SETUP_EVALUATION":
        raise DecisionChainStateError("A/B/C candidates require SETUP_EVALUATION")
    return {
        "mapped_pattern": pattern,
        "probability_evidence": probability,
        "payoff_evidence": payoff,
        "course_grade": grade,
        "grade_reason": _text(value.get("grade_reason"), "opportunity_context.grade_reason", 300),
        "expected_behavior": _text(value.get("expected_behavior"), "opportunity_context.expected_behavior", 300),
        "max_wait_bars": max_wait,
        "behavior_invalidation": _text(
            value.get("behavior_invalidation"), "opportunity_context.behavior_invalidation", 300
        ),
        "recovery_round_policy": recovery,
    }


def _material(value: Mapping[str, Any]) -> tuple[Any, ...]:
    opening = value["opening_context"]
    structure = value["structure_context"]
    opportunity = value["opportunity_context"]
    return (
        value["process_stage"],
        tuple((key, opening[key]) for key in opening),
        tuple((key, tuple(structure[key]) if key == "confluences" else structure[key]) for key in structure),
        tuple((key, opportunity[key]) for key in opportunity),
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
            raise DecisionChainStateError(f"{field} must advance when its state changes")
    elif before != after:
        raise DecisionChainStateError(f"{field} changed without a state change")


def _enum(value: Any, allowed: set[str], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise DecisionChainStateError(f"{field} is invalid")
    return value


def _text(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise DecisionChainStateError(f"{field} must be text")
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > max_length:
        raise DecisionChainStateError(f"{field} has invalid length")
    return cleaned


def _optional_positive_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise DecisionChainStateError(f"{field} must be a positive number or null")
    return float(value)


def _iso_time(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise DecisionChainStateError(f"{field} must be an ISO timestamp")
    parsed = _parse_time(value, field)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DecisionChainStateError(f"{field} must include a timezone")
    return value


def _optional_time(value: Any, field: str) -> str | None:
    return None if value is None else _iso_time(value, field)


def _parse_time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise DecisionChainStateError(f"{field} is not a valid ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DecisionChainStateError(f"{field} must include a timezone")
    return parsed
