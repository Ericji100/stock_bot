from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Mapping


ANCHOR_LIMIT = 32
ANCHOR_GRADES = {"LARGE", "SMALL"}
ANCHOR_DIRECTIONS = {"BULL", "BEAR"}
ANCHOR_STATUSES = {"FORMING", "CONFIRMED", "INVALIDATED", "REPLACED"}
TERMINAL_ANCHOR_STATUSES = {"INVALIDATED", "REPLACED"}
ANCHOR_SOURCES = {
    "OPENING_FIRST_VALID",
    "OR_BREAK",
    "SESSION_EXTREME_BREAK",
    "STRUCTURE_BREAK",
    "DEFENSE_BREAK",
    "TYPE_3_DOUBLE_DEFENSE",
    "HEAD_BOTTOM_SEQUENCE",
}
ANCHOR_QUALITY = {"CLEAN", "CAUTION", "UNQUALIFIED", "UNKNOWN"}
DESTRUCTIVE_EVIDENCE = {
    "NONE",
    "OPENING_DIRECTION_HOLD",
    "OR_BREAK",
    "SESSION_EXTREME_BREAK",
    "STRUCTURE_BREAK",
    "SMALL_DEFENSE_BREAK",
    "LARGE_DEFENSE_BREAK",
    "TYPE_3_DOUBLE_DEFENSE",
    "HEAD_BOTTOM_SEQUENCE",
}
CONTROLLING_GRADES = {"LARGE", "SMALL", "UNDEFINED"}
GRADE_RELATIONS = {"ALIGNED", "CONFLICT", "ONLY_LARGE", "ONLY_SMALL", "UNDEFINED"}
TREND_DYNAMICS = {"INCREASING", "DECREASING", "UNCLEAR"}
VOLATILITY_DYNAMICS = {"EXPANDING", "CONTRACTING", "UNSTABLE", "UNCLEAR"}
WORKING_QUADRANTS = {"Q1", "Q2", "Q3", "Q4", "TRANSITION", "UNDEFINED"}
QUADRANT_CANDIDATES = {"Q1", "Q2", "Q3", "Q4", "UNDEFINED"}
ZONE_RELIABILITY = {"DIRECT", "ESTIMATED", "UNAVAILABLE"}

_TREND_COMPATIBLE_QUADRANTS = {
    "INCREASING": {"Q1", "Q4"},
    "DECREASING": {"Q2", "Q3"},
    "UNCLEAR": {"Q1", "Q2", "Q3", "Q4"},
}
_VOLATILITY_COMPATIBLE_QUADRANTS = {
    "EXPANDING": {"Q1", "Q2"},
    "CONTRACTING": {"Q3", "Q4"},
    "UNSTABLE": {"Q1", "Q2", "Q3", "Q4"},
    "UNCLEAR": {"Q1", "Q2", "Q3", "Q4"},
}

_ADJACENT_QUADRANTS = {
    "Q1": {"Q2", "Q4"},
    "Q2": {"Q1", "Q3"},
    "Q3": {"Q2", "Q4"},
    "Q4": {"Q1", "Q3"},
}

_LARGE_PROMOTION_EVIDENCE = {"LARGE_DEFENSE_BREAK", "TYPE_3_DOUBLE_DEFENSE"}

ANCHOR_CONTEXT_KEYS = {
    "anchors",
    "active_large_anchor_id",
    "active_small_anchor_id",
    "controlling_grade",
    "grade_relation",
    "control_reason",
    "control_changed_at",
    "trend_dynamics",
    "volatility_dynamics",
    "working_quadrant",
    "primary_quadrant_candidate",
    "secondary_quadrant_candidate",
    "eliminated_quadrants",
    "quadrant_reason",
    "quadrant_changed_at",
}


class AnchorStateError(ValueError):
    pass


def empty_anchor_context() -> dict[str, Any]:
    return {
        "anchors": [],
        "active_large_anchor_id": None,
        "active_small_anchor_id": None,
        "controlling_grade": "UNDEFINED",
        "grade_relation": "UNDEFINED",
        "control_reason": "尚無已確認的工作錨。",
        "control_changed_at": None,
        "trend_dynamics": "UNCLEAR",
        "volatility_dynamics": "UNCLEAR",
        "working_quadrant": "UNDEFINED",
        "primary_quadrant_candidate": "UNDEFINED",
        "secondary_quadrant_candidate": None,
        "eliminated_quadrants": [],
        "quadrant_reason": "錨與後續波段不足，暫不分類象限。",
        "quadrant_changed_at": None,
    }


def normalize_legacy_quadrant_axis_consistency(value: Mapping[str, Any]) -> dict[str, Any]:
    """Repair a persisted pre-v2.1.4 primary candidate without inventing an axis.

    Earlier releases could combine trend evidence from the controlling small
    grade with volatility evidence from the large background.  Prefer an
    already-retained compatible secondary candidate.  If none exists, leave
    the primary undefined so the next analysis must classify it again.
    """
    result = deepcopy(dict(value))
    trend = result.get("trend_dynamics")
    volatility = result.get("volatility_dynamics")
    primary = result.get("primary_quadrant_candidate")
    working = result.get("working_quadrant")
    if trend not in _TREND_COMPATIBLE_QUADRANTS or volatility not in _VOLATILITY_COMPATIBLE_QUADRANTS:
        return result
    compatible = _TREND_COMPATIBLE_QUADRANTS[trend] & _VOLATILITY_COMPATIBLE_QUADRANTS[volatility]
    if primary not in {"Q1", "Q2", "Q3", "Q4"} or primary in compatible:
        return result

    secondary = result.get("secondary_quadrant_candidate")
    replacement = secondary if secondary in compatible else None
    result["primary_quadrant_candidate"] = replacement or "UNDEFINED"
    result["secondary_quadrant_candidate"] = primary if replacement is not None else None
    if working in {"Q1", "Q2", "Q3", "Q4"} and working not in compatible:
        result["working_quadrant"] = "TRANSITION"

    candidates = {
        item
        for item in (
            result["primary_quadrant_candidate"],
            result["secondary_quadrant_candidate"],
        )
        if item in {"Q1", "Q2", "Q3", "Q4"}
    }
    result["eliminated_quadrants"] = [
        item for item in result.get("eliminated_quadrants", []) if item not in candidates
    ]
    result["quadrant_reason"] = (
        "舊版曾混用不同級數的趨勢與波動證據；已保留既有軸資料，"
        "並要求本輪依目前控制級數重新判定工作象限。"
    )
    return result


def validate_anchor_context(value: Any, *, as_of: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != ANCHOR_CONTEXT_KEYS:
        raise AnchorStateError("anchor_context has invalid fields")
    ceiling = _parse_time(as_of, "as_of")
    anchors_value = value.get("anchors")
    if not isinstance(anchors_value, list) or len(anchors_value) > ANCHOR_LIMIT:
        raise AnchorStateError(f"anchor_context.anchors must contain at most {ANCHOR_LIMIT} items")
    anchors = [_anchor(item, as_of=as_of) for item in anchors_value]
    anchor_ids = [item["anchor_id"] for item in anchors]
    if len(set(anchor_ids)) != len(anchor_ids):
        raise AnchorStateError("anchor ids must be unique")
    first_seen_times = [_parse_time(item["first_seen_at"], "anchor.first_seen_at") for item in anchors]
    if first_seen_times != sorted(first_seen_times):
        raise AnchorStateError("anchors must be ordered by first_seen_at")
    by_id = {item["anchor_id"]: item for item in anchors}

    active_large = _optional_text(value.get("active_large_anchor_id"), "active_large_anchor_id", 120)
    active_small = _optional_text(value.get("active_small_anchor_id"), "active_small_anchor_id", 120)
    _validate_active_anchor(active_large, "LARGE", by_id)
    _validate_active_anchor(active_small, "SMALL", by_id)

    controlling = _enum(value.get("controlling_grade"), CONTROLLING_GRADES, "controlling_grade")
    relation = _enum(value.get("grade_relation"), GRADE_RELATIONS, "grade_relation")
    if controlling == "LARGE" and active_large is None:
        raise AnchorStateError("LARGE control requires an active large anchor")
    if controlling == "SMALL" and active_small is None:
        raise AnchorStateError("SMALL control requires an active small anchor")
    if not anchors and (controlling != "UNDEFINED" or relation != "UNDEFINED"):
        raise AnchorStateError("empty anchor context must use undefined control")
    control_changed_at = _optional_time(value.get("control_changed_at"), "control_changed_at")
    if controlling == "UNDEFINED" and active_large is None and active_small is None:
        if control_changed_at is not None:
            raise AnchorStateError("undefined empty control must not have control_changed_at")
    elif control_changed_at is None or _parse_time(control_changed_at, "control_changed_at") > ceiling:
        raise AnchorStateError("active control requires a causal control_changed_at")

    working = _enum(value.get("working_quadrant"), WORKING_QUADRANTS, "working_quadrant")
    primary = _enum(
        value.get("primary_quadrant_candidate"),
        QUADRANT_CANDIDATES,
        "primary_quadrant_candidate",
    )
    secondary_value = value.get("secondary_quadrant_candidate")
    secondary = None if secondary_value is None else _enum(
        secondary_value,
        QUADRANT_CANDIDATES - {"UNDEFINED"},
        "secondary_quadrant_candidate",
    )
    eliminated_value = value.get("eliminated_quadrants")
    if not isinstance(eliminated_value, list) or len(eliminated_value) > 4:
        raise AnchorStateError("eliminated_quadrants must be a list with at most four items")
    eliminated = [_enum(item, {"Q1", "Q2", "Q3", "Q4"}, "eliminated_quadrants item") for item in eliminated_value]
    if len(set(eliminated)) != len(eliminated):
        raise AnchorStateError("eliminated_quadrants must not contain duplicates")
    if primary in eliminated or secondary in eliminated:
        raise AnchorStateError("quadrant candidates cannot also be eliminated")
    if secondary is not None and secondary == primary:
        raise AnchorStateError("primary and secondary quadrant candidates must differ")
    if (
        primary in _ADJACENT_QUADRANTS
        and secondary is not None
        and secondary not in _ADJACENT_QUADRANTS[primary]
    ):
        raise AnchorStateError(
            "the secondary quadrant candidate must be reachable by changing exactly one axis"
        )
    if working in {"Q1", "Q2", "Q3", "Q4"} and primary != working:
        raise AnchorStateError("a confirmed working quadrant must be the primary candidate")
    if not anchors and (
        working != "UNDEFINED"
        or primary != "UNDEFINED"
        or secondary is not None
        or eliminated
    ):
        raise AnchorStateError("quadrant candidates require at least one causal anchor")
    quadrant_changed_at = _optional_time(value.get("quadrant_changed_at"), "quadrant_changed_at")
    if working == "UNDEFINED" and primary == "UNDEFINED" and secondary is None and not eliminated:
        if quadrant_changed_at is not None:
            raise AnchorStateError("undefined quadrant state must not have quadrant_changed_at")
    elif quadrant_changed_at is None or _parse_time(quadrant_changed_at, "quadrant_changed_at") > ceiling:
        raise AnchorStateError("classified quadrant state requires a causal quadrant_changed_at")

    trend_dynamics = _enum(value.get("trend_dynamics"), TREND_DYNAMICS, "trend_dynamics")
    volatility_dynamics = _enum(
        value.get("volatility_dynamics"), VOLATILITY_DYNAMICS, "volatility_dynamics"
    )
    compatible_quadrants = (
        _TREND_COMPATIBLE_QUADRANTS[trend_dynamics]
        & _VOLATILITY_COMPATIBLE_QUADRANTS[volatility_dynamics]
    )
    # The primary/working quadrant describes current evidence.  The secondary
    # candidate may intentionally describe the adjacent state that would take
    # over if one axis changes, so it is not constrained to the current axes.
    classified = [
        quadrant
        for quadrant in (working, primary)
        if quadrant in {"Q1", "Q2", "Q3", "Q4"}
    ]
    incompatible = [quadrant for quadrant in classified if quadrant not in compatible_quadrants]
    if incompatible:
        raise AnchorStateError(
            "quadrant candidates must use trend and volatility evidence from the controlling grade"
        )

    return {
        "anchors": anchors,
        "active_large_anchor_id": active_large,
        "active_small_anchor_id": active_small,
        "controlling_grade": controlling,
        "grade_relation": relation,
        "control_reason": _text(value.get("control_reason"), "control_reason", 240),
        "control_changed_at": control_changed_at,
        "trend_dynamics": trend_dynamics,
        "volatility_dynamics": volatility_dynamics,
        "working_quadrant": working,
        "primary_quadrant_candidate": primary,
        "secondary_quadrant_candidate": secondary,
        "eliminated_quadrants": eliminated,
        "quadrant_reason": _text(value.get("quadrant_reason"), "quadrant_reason", 240),
        "quadrant_changed_at": quadrant_changed_at,
    }


def validate_anchor_hierarchy_semantics(value: Any, *, as_of: str) -> dict[str, Any]:
    """Validate parent/child grade semantics for a newly produced snapshot.

    ``validate_anchor_context`` intentionally remains backward-compatible so a
    persisted snapshot from an older release can still be read and repaired on
    the next bar.  This stricter validator is for new model output: it prevents
    a long, session-extreme-breaking movement from being retained as the only
    small anchor and requires active large/small anchors to form one explicit
    hierarchy.
    """

    context = validate_anchor_context(value, as_of=as_of)
    by_id = {item["anchor_id"]: item for item in context["anchors"]}

    for anchor in context["anchors"]:
        parent_id = anchor["parent_anchor_id"]
        if anchor["grade"] == "LARGE":
            if parent_id is not None:
                raise AnchorStateError("a large anchor cannot have a parent anchor")
            continue
        if parent_id is None:
            continue
        parent = by_id.get(parent_id)
        if parent is None or parent["grade"] != "LARGE":
            raise AnchorStateError("a small anchor parent must reference a retained large anchor")
        if _parse_time(parent["start_bar_time"], "parent.start_bar_time") > _parse_time(
            anchor["start_bar_time"], "anchor.start_bar_time"
        ):
            raise AnchorStateError("a child anchor cannot start before its large parent")
        if (
            anchor["status"] not in TERMINAL_ANCHOR_STATUSES
            and parent["status"] in TERMINAL_ANCHOR_STATUSES
        ):
            raise AnchorStateError("an active small anchor cannot reference a terminal large parent")

    active_large_id = context["active_large_anchor_id"]
    active_small_id = context["active_small_anchor_id"]
    if active_large_id is not None and active_small_id is not None:
        large = by_id[active_large_id]
        small = by_id[active_small_id]
        expected_relation = "ALIGNED" if large["direction"] == small["direction"] else "CONFLICT"
        if context["grade_relation"] != expected_relation:
            raise AnchorStateError("active large/small anchor directions do not match grade_relation")
        if small["parent_anchor_id"] != active_large_id:
            raise AnchorStateError("the active small anchor must reference the active large parent")
    elif active_large_id is not None:
        if context["grade_relation"] != "ONLY_LARGE":
            raise AnchorStateError("a lone active large anchor requires ONLY_LARGE grade_relation")
    elif active_small_id is not None:
        if context["grade_relation"] != "ONLY_SMALL":
            raise AnchorStateError("a lone active small anchor requires ONLY_SMALL grade_relation")
    elif context["grade_relation"] != "UNDEFINED":
        raise AnchorStateError("no active anchors requires UNDEFINED grade_relation")

    if active_large_id is not None:
        large = by_id[active_large_id]
        large_start = _parse_time(large["start_bar_time"], "large.start_bar_time")
        evidence = set(large["destructive_evidence"])
        is_promoted_large_move = bool(evidence & _LARGE_PROMOTION_EVIDENCE) or {
            "SESSION_EXTREME_BREAK",
            "STRUCTURE_BREAK",
        }.issubset(evidence)
        if is_promoted_large_move:
            preceding_opposite_extremes = []
            for anchor in context["anchors"]:
                if (
                    anchor["anchor_id"] == active_large_id
                    or anchor["direction"] == large["direction"]
                    or anchor["status"] not in TERMINAL_ANCHOR_STATUSES
                ):
                    continue
                opposite_extreme = _parse_time(
                    anchor["extreme_bar_time"], "opposite.extreme_bar_time"
                )
                terminal_at = _parse_time(anchor["terminal_at"], "opposite.terminal_at")
                if opposite_extreme < large_start <= terminal_at:
                    preceding_opposite_extremes.append(opposite_extreme)
            if preceding_opposite_extremes:
                raise AnchorStateError(
                    "a promoted large anchor must start at or before the preceding opposite anchor extreme"
                )

    if active_small_id is not None and active_large_id is None:
        small = by_id[active_small_id]
        duration_minutes = (
            _parse_time(small["extreme_bar_time"], "anchor.extreme_bar_time")
            - _parse_time(small["start_bar_time"], "anchor.start_bar_time")
        ).total_seconds() / 60.0
        evidence = set(small["destructive_evidence"])
        explicit_large_break = bool(
            evidence & {"LARGE_DEFENSE_BREAK", "TYPE_3_DOUBLE_DEFENSE"}
        )
        broad_session_structure = (
            duration_minutes >= 30
            and {"SESSION_EXTREME_BREAK", "STRUCTURE_BREAK"}.issubset(evidence)
        )
        if explicit_large_break or broad_session_structure:
            raise AnchorStateError(
                "a broad session-extreme-breaking small anchor requires an active large parent"
            )

    return context


def validate_anchor_transition(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    prior_as_of: str,
    current_as_of: str,
) -> None:
    before = validate_anchor_context(previous, as_of=prior_as_of)
    after = validate_anchor_context(current, as_of=current_as_of)
    prior_time = _parse_time(prior_as_of, "prior_as_of")
    old = {item["anchor_id"]: item for item in before["anchors"]}
    new = {item["anchor_id"]: item for item in after["anchors"]}
    for anchor_id, old_anchor in old.items():
        new_anchor = new.get(anchor_id)
        if new_anchor is None:
            raise AnchorStateError("anchors cannot disappear within the same session")
        _validate_anchor_item_transition(old_anchor, new_anchor)
    for anchor_id, new_anchor in new.items():
        if anchor_id not in old and _parse_time(new_anchor["first_seen_at"], "first_seen_at") <= prior_time:
            raise AnchorStateError("new anchors cannot backdate first_seen_at")

    for field in ("active_large_anchor_id", "active_small_anchor_id"):
        before_id = before[field]
        after_id = after[field]
        if before_id is not None and before_id != after_id:
            closed = new.get(before_id)
            if closed is None or closed["status"] not in TERMINAL_ANCHOR_STATUSES:
                raise AnchorStateError("an active anchor must terminate before control switches")

    control_signature_before = (before["controlling_grade"], before["grade_relation"])
    control_signature_after = (after["controlling_grade"], after["grade_relation"])
    _validate_changed_at(
        changed=control_signature_before != control_signature_after,
        before=before["control_changed_at"],
        after=after["control_changed_at"],
        prior_time=prior_time,
        field="control_changed_at",
    )
    quadrant_signature_before = (
        before["working_quadrant"],
        before["primary_quadrant_candidate"],
        before["secondary_quadrant_candidate"],
        tuple(before["eliminated_quadrants"]),
    )
    quadrant_signature_after = (
        after["working_quadrant"],
        after["primary_quadrant_candidate"],
        after["secondary_quadrant_candidate"],
        tuple(after["eliminated_quadrants"]),
    )
    _validate_changed_at(
        changed=quadrant_signature_before != quadrant_signature_after,
        before=before["quadrant_changed_at"],
        after=after["quadrant_changed_at"],
        prior_time=prior_time,
        field="quadrant_changed_at",
    )


def anchor_notification_reasons(previous: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    before = previous
    after = current
    reasons: list[str] = []
    old = {item["anchor_id"]: item for item in before["anchors"]}
    new = {item["anchor_id"]: item for item in after["anchors"]}
    if set(new) - set(old):
        reasons.append("定錨候選建立")
    if any(old[key]["status"] != new[key]["status"] for key in set(old) & set(new)):
        reasons.append("定錨確認或失效")
    if any(before[key] != after[key] for key in ("active_large_anchor_id", "active_small_anchor_id")):
        reasons.append("作用中定錨切換")
    if any(before[key] != after[key] for key in ("controlling_grade", "grade_relation")):
        reasons.append("大小級控制權改變")
    if any(
        before[key] != after[key]
        for key in (
            "working_quadrant",
            "primary_quadrant_candidate",
            "secondary_quadrant_candidate",
            "eliminated_quadrants",
        )
    ):
        reasons.append("動態象限看法改變")
    return reasons


def _anchor(value: Any, *, as_of: str) -> dict[str, Any]:
    keys = {
        "anchor_id",
        "grade",
        "direction",
        "status",
        "source",
        "start_bar_time",
        "extreme_bar_time",
        "start_price_estimate",
        "extreme_price_estimate",
        "first_seen_at",
        "confirmed_at",
        "terminal_at",
        "replaced_by",
        "quality",
        "destructive_evidence",
        "invalidation_zone",
        "parent_anchor_id",
        "notes",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise AnchorStateError("anchor has invalid fields")
    ceiling = _parse_time(as_of, "as_of")
    status = _enum(value.get("status"), ANCHOR_STATUSES, "anchor.status")
    direction = _enum(value.get("direction"), ANCHOR_DIRECTIONS, "anchor.direction")
    start_time = _iso_time(value.get("start_bar_time"), "anchor.start_bar_time")
    extreme_time = _iso_time(value.get("extreme_bar_time"), "anchor.extreme_bar_time")
    first_seen = _iso_time(value.get("first_seen_at"), "anchor.first_seen_at")
    confirmed = _optional_time(value.get("confirmed_at"), "anchor.confirmed_at")
    terminal = _optional_time(value.get("terminal_at"), "anchor.terminal_at")
    times = [start_time, extreme_time, first_seen, *[item for item in (confirmed, terminal) if item]]
    if any(_parse_time(item, "anchor timestamp") > ceiling for item in times):
        raise AnchorStateError("anchor contains a future timestamp")
    if _parse_time(extreme_time, "extreme_bar_time") < _parse_time(start_time, "start_bar_time"):
        raise AnchorStateError("anchor extreme cannot predate its start")
    if confirmed and _parse_time(confirmed, "confirmed_at") < _parse_time(first_seen, "first_seen_at"):
        raise AnchorStateError("anchor confirmation cannot predate first_seen_at")
    if terminal and _parse_time(terminal, "terminal_at") < _parse_time(first_seen, "first_seen_at"):
        raise AnchorStateError("anchor terminal_at cannot predate first_seen_at")
    if status == "FORMING" and (confirmed is not None or terminal is not None):
        raise AnchorStateError("forming anchor cannot be confirmed or terminal")
    if status == "CONFIRMED" and (confirmed is None or terminal is not None):
        raise AnchorStateError("confirmed anchor requires confirmed_at and no terminal_at")
    if status in TERMINAL_ANCHOR_STATUSES and terminal is None:
        raise AnchorStateError("terminal anchor requires terminal_at")

    replaced_by = value.get("replaced_by")
    if status == "REPLACED":
        replaced_by = _text(replaced_by, "anchor.replaced_by", 120)
    elif replaced_by is not None:
        # INVALIDATED and other non-replacement terminal states may arrive with
        # the newly formed opposite anchor copied into replaced_by.  The status
        # is the market decision; the pointer is only mechanical bookkeeping and
        # has no meaning unless status is REPLACED.
        replaced_by = None
    quality = _enum(value.get("quality"), ANCHOR_QUALITY, "anchor.quality")
    if status == "CONFIRMED" and quality == "UNQUALIFIED":
        raise AnchorStateError("an unqualified anchor cannot be confirmed")
    evidence_value = value.get("destructive_evidence")
    if not isinstance(evidence_value, list) or not 1 <= len(evidence_value) <= 5:
        raise AnchorStateError("anchor destructive_evidence must contain one to five items")
    evidence = [_enum(item, DESTRUCTIVE_EVIDENCE, "anchor.destructive_evidence item") for item in evidence_value]
    if len(set(evidence)) != len(evidence) or ("NONE" in evidence and len(evidence) != 1):
        raise AnchorStateError("anchor destructive_evidence is inconsistent")
    if status == "CONFIRMED" and evidence == ["NONE"]:
        raise AnchorStateError("confirmed anchor requires destructive evidence")

    start_price = _optional_positive_number(value.get("start_price_estimate"), "anchor.start_price_estimate")
    extreme_price = _optional_positive_number(value.get("extreme_price_estimate"), "anchor.extreme_price_estimate")
    if start_price is not None and extreme_price is not None:
        if direction == "BULL" and extreme_price < start_price:
            raise AnchorStateError("bull anchor extreme cannot be below its start")
        if direction == "BEAR" and extreme_price > start_price:
            raise AnchorStateError("bear anchor extreme cannot be above its start")
    notes_value = value.get("notes")
    if not isinstance(notes_value, list) or not 1 <= len(notes_value) <= 3:
        raise AnchorStateError("anchor.notes must contain one to three items")
    return {
        "anchor_id": _text(value.get("anchor_id"), "anchor.anchor_id", 120),
        "grade": _enum(value.get("grade"), ANCHOR_GRADES, "anchor.grade"),
        "direction": direction,
        "status": status,
        "source": _enum(value.get("source"), ANCHOR_SOURCES, "anchor.source"),
        "start_bar_time": start_time,
        "extreme_bar_time": extreme_time,
        "start_price_estimate": start_price,
        "extreme_price_estimate": extreme_price,
        "first_seen_at": first_seen,
        "confirmed_at": confirmed,
        "terminal_at": terminal,
        "replaced_by": replaced_by,
        "quality": quality,
        "destructive_evidence": evidence,
        "invalidation_zone": _price_zone(value.get("invalidation_zone"), "anchor.invalidation_zone"),
        "parent_anchor_id": _optional_text(value.get("parent_anchor_id"), "anchor.parent_anchor_id", 120),
        "notes": [_text(item, "anchor.notes item", 200) for item in notes_value],
    }


def _validate_active_anchor(anchor_id: str | None, grade: str, by_id: Mapping[str, Mapping[str, Any]]) -> None:
    if anchor_id is None:
        return
    anchor = by_id.get(anchor_id)
    if anchor is None or anchor["grade"] != grade or anchor["status"] in TERMINAL_ANCHOR_STATUSES:
        raise AnchorStateError(f"active {grade.lower()} anchor id is invalid")


def _validate_anchor_item_transition(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    for immutable in (
        "anchor_id",
        "grade",
        "direction",
        "source",
        "start_bar_time",
        "start_price_estimate",
        "first_seen_at",
        "parent_anchor_id",
    ):
        if before[immutable] != after[immutable]:
            raise AnchorStateError(f"anchor changed immutable field {immutable}")
    allowed = {
        "FORMING": {"FORMING", "CONFIRMED", "INVALIDATED", "REPLACED"},
        "CONFIRMED": {"CONFIRMED", "INVALIDATED", "REPLACED"},
        "INVALIDATED": {"INVALIDATED"},
        "REPLACED": {"REPLACED"},
    }
    if after["status"] not in allowed[before["status"]]:
        raise AnchorStateError("anchor has an invalid status transition")
    for timestamp in ("confirmed_at", "terminal_at"):
        if before[timestamp] is not None and before[timestamp] != after[timestamp]:
            raise AnchorStateError(f"anchor changed its first {timestamp}")
    before_extreme_time = _parse_time(before["extreme_bar_time"], "before.extreme_bar_time")
    after_extreme_time = _parse_time(after["extreme_bar_time"], "after.extreme_bar_time")
    if after_extreme_time < before_extreme_time:
        raise AnchorStateError("anchor extreme time cannot move backward")
    before_price = before["extreme_price_estimate"]
    after_price = after["extreme_price_estimate"]
    if before_price is not None and after_price is not None and before["status"] not in TERMINAL_ANCHOR_STATUSES:
        if before["direction"] == "BULL" and after_price < before_price:
            raise AnchorStateError("bull anchor extreme cannot retreat")
        if before["direction"] == "BEAR" and after_price > before_price:
            raise AnchorStateError("bear anchor extreme cannot retreat")
    if before["status"] in TERMINAL_ANCHOR_STATUSES and before != after:
        raise AnchorStateError("terminal anchors are immutable")


def _validate_changed_at(*, changed: bool, before: str | None, after: str | None, prior_time: datetime, field: str) -> None:
    if changed:
        # A validated empty/undefined target is represented by a null timestamp.
        # Allow that explicit reset; active targets are required to carry a time
        # by validate_anchor_context before transition validation reaches here.
        if after is None:
            return
        if _parse_time(after, field) <= prior_time:
            raise AnchorStateError(f"{field} must advance when its state changes")
    elif before != after:
        raise AnchorStateError(f"unchanged state cannot rewrite {field}")


def _price_zone(value: Any, field: str) -> dict[str, Any]:
    keys = {"low", "high", "reliability", "reason"}
    if not isinstance(value, Mapping) or set(value) != keys:
        raise AnchorStateError(f"{field} has invalid fields")
    reliability = _enum(value.get("reliability"), ZONE_RELIABILITY, f"{field}.reliability")
    low = _optional_positive_number(value.get("low"), f"{field}.low")
    high = _optional_positive_number(value.get("high"), f"{field}.high")
    if reliability == "UNAVAILABLE":
        if low is not None or high is not None:
            raise AnchorStateError(f"{field} unavailable zone must not contain prices")
    elif low is None or high is None or low > high:
        raise AnchorStateError(f"{field} available zone must contain an ordered range")
    return {
        "low": low,
        "high": high,
        "reliability": reliability,
        "reason": _text(value.get("reason"), f"{field}.reason", 160),
    }


def _enum(value: Any, allowed: set[str], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise AnchorStateError(f"{field} is invalid")
    return value


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise AnchorStateError(f"{field} is invalid")
    return value.strip()


def _optional_text(value: Any, field: str, maximum: int) -> str | None:
    return None if value is None else _text(value, field, maximum)


def _optional_positive_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise AnchorStateError(f"{field} must be a positive number or null")
    return float(value)


def _optional_time(value: Any, field: str) -> str | None:
    return None if value is None else _iso_time(value, field)


def _iso_time(value: Any, field: str) -> str:
    return _parse_time(value, field).isoformat()


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise AnchorStateError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AnchorStateError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AnchorStateError(f"{field} must include a timezone")
    return parsed
