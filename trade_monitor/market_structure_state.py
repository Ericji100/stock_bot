from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Mapping

from .anchor_state import (
    AnchorStateError,
    anchor_notification_reasons,
    empty_anchor_context,
    validate_anchor_context,
    validate_anchor_hierarchy_semantics,
    validate_anchor_transition,
)
from .cclass_state import (
    CClassStateError,
    cclass_notification_reasons,
    empty_cclass_context,
    validate_cclass_context,
    validate_cclass_transition,
)
from .decision_chain_state import (
    DecisionChainStateError,
    decision_chain_notification_reasons,
    empty_decision_chain_context,
    validate_decision_chain_context,
    validate_decision_chain_transition,
)
from .prospective_state import (
    ProspectiveStateError,
    empty_prospective_context,
    prospective_notification_reasons,
    validate_prospective_context,
    validate_prospective_transition,
    validate_taiji_evolution_alignment,
)
from .strategy_catalog import (
    QUADRANT_METHODS,
    STRATEGY_METHODS,
    TAIJI_METHODS,
    YIZHI_METHODS,
)

STATE_VERSION = 6
DECISION_CHAIN_STATE_VERSION = 5
CCLASS_STATE_VERSION = 4
ANCHOR_STATE_VERSION = 3
PREVIOUS_STATE_VERSION = 2
LEGACY_STATE_VERSION = 1
PRIMARY_PIVOT_LIMIT = 96
SECONDARY_PIVOT_LIMIT = 48
PIVOT_KINDS = {"HIGH", "LOW"}
PIVOT_STATES = {"CANDIDATE", "LOCAL_CONFIRMED", "PAIRED_CONFIRMED", "INVALIDATED", "REPLACED"}
TERMINAL_PIVOT_STATES = {"INVALIDATED", "REPLACED"}
PROPORTIONALITY = {"NORMAL", "MICRO", "SUSPECT", "UNKNOWN"}
DOW_STATES = {"BULL", "BEAR", "TRANSITION", "UNDEFINED"}
REVERSAL_TYPES = {"NONE", "TYPE_1", "TYPE_2", "TYPE_3"}
MATURITY_STATES = {"NONE", "LEFT_LEFT", "LEFT_RIGHT", "RIGHT_LEFT", "RIGHT_RIGHT"}
DEFENSE_STATUS = {"ACTIVE", "BROKEN", "REPLACED"}
BOUNDARY_KINDS = {"OR_OPPOSITE", "STRUCTURE_BOUNDARY", "SESSION_EXTREME"}
WAVE_PHASES = {"INITIAL", "MIDDLE", "LATE", "PULLBACK", "REBOUND", "TRANSITION", "RANGE", "UNDEFINED"}
LOCATIONS = {
    "NEAR_SUPPORT", "NEAR_RESISTANCE", "PRIOR_HIGH", "PRIOR_LOW", "RANGE_EDGE",
    "RANGE_MIDDLE", "BREAKOUT_RETEST", "DEFENSE_RETEST", "UNDEFINED",
}
GRADE_ALIGNMENT = {"ALIGNED", "PARTIAL", "MISALIGNED", "UNDEFINED"}
PATTERN_TYPES = STRATEGY_METHODS
SETUP_STAGES = {
    "NONE", "FORMING", "ARMED", "AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED",
    "INVALIDATED", "NO_CHASE",
}
TERMINAL_SETUP_STAGES = {"INVALIDATED", "NO_CHASE"}
ZONE_RELIABILITY = {"DIRECT", "ESTIMATED", "UNAVAILABLE"}
DEFENSE_SLOTS = ("small_bull", "small_bear", "large_bull", "large_bear")

TOP_LEVEL_KEYS_V2 = {
    "version",
    "as_of",
    "session_key",
    "primary_pivots",
    "secondary_pivots",
    "defense_lines",
    "provisional_invalidation_boundaries",
    "dow_state_small",
    "dow_state_large",
    "reversal_type",
    "maturity",
    "scenario_context",
    "notes",
}
TOP_LEVEL_KEYS_V3 = TOP_LEVEL_KEYS_V2 | {"anchor_context"}
TOP_LEVEL_KEYS_V4 = TOP_LEVEL_KEYS_V3 | {"cclass_context"}
TOP_LEVEL_KEYS_V5 = TOP_LEVEL_KEYS_V4 | {"decision_chain_context"}
TOP_LEVEL_KEYS_V6 = TOP_LEVEL_KEYS_V5 | {"prospective_context"}

LEGACY_TOP_LEVEL_KEYS = {
    "version", "as_of", "session_key", "primary_pivots", "secondary_pivots",
    "small_defense_line", "large_defense_line", "provisional_invalidation_boundary",
    "dow_state_small", "dow_state_large", "reversal_type", "maturity", "notes",
}


class MarketStructureStateError(ValueError):
    pass


def empty_market_structure_state(
    *,
    as_of: str,
    session_key: str,
    version: int = PREVIOUS_STATE_VERSION,
) -> dict[str, Any]:
    if version not in {
        PREVIOUS_STATE_VERSION,
        ANCHOR_STATE_VERSION,
        CCLASS_STATE_VERSION,
        DECISION_CHAIN_STATE_VERSION,
        STATE_VERSION,
    }:
        raise MarketStructureStateError("requested market structure state version is invalid")
    result = {
        "version": version,
        "as_of": _iso_time(as_of, "as_of"),
        "session_key": _text(session_key, "session_key", 80),
        "primary_pivots": [],
        "secondary_pivots": [],
        "defense_lines": {slot: None for slot in DEFENSE_SLOTS},
        "provisional_invalidation_boundaries": {"bull": None, "bear": None},
        "dow_state_small": "UNDEFINED",
        "dow_state_large": "UNDEFINED",
        "reversal_type": "NONE",
        "maturity": "NONE",
        "scenario_context": _empty_scenario_context(),
        "notes": ["尚無已確認樞紐。"],
    }
    if version >= ANCHOR_STATE_VERSION:
        result["anchor_context"] = empty_anchor_context()
    if version >= CCLASS_STATE_VERSION:
        result["cclass_context"] = empty_cclass_context(as_of=as_of)
    if version >= DECISION_CHAIN_STATE_VERSION:
        result["decision_chain_context"] = empty_decision_chain_context(as_of=as_of)
    if version == STATE_VERSION:
        result["prospective_context"] = empty_prospective_context(as_of=as_of)
    return result


def validate_market_structure_state(
    value: Mapping[str, Any],
    *,
    expected_as_of: str | None = None,
) -> dict[str, Any]:
    if isinstance(value, Mapping) and value.get("version") == LEGACY_STATE_VERSION:
        value = _migrate_legacy_state(value)
    if not isinstance(value, Mapping):
        raise MarketStructureStateError("market_structure_state has invalid top-level fields")
    version = value.get("version")
    expected_keys = (
        TOP_LEVEL_KEYS_V6
        if version == STATE_VERSION
        else TOP_LEVEL_KEYS_V5
        if version == DECISION_CHAIN_STATE_VERSION
        else TOP_LEVEL_KEYS_V4
        if version == CCLASS_STATE_VERSION
        else TOP_LEVEL_KEYS_V3
        if version == ANCHOR_STATE_VERSION
        else TOP_LEVEL_KEYS_V2
    )
    if set(value) != expected_keys:
        raise MarketStructureStateError("market_structure_state has invalid top-level fields")
    if version not in {
        PREVIOUS_STATE_VERSION,
        ANCHOR_STATE_VERSION,
        CCLASS_STATE_VERSION,
        DECISION_CHAIN_STATE_VERSION,
        STATE_VERSION,
    }:
        raise MarketStructureStateError("market_structure_state version is invalid")

    as_of = _iso_time(value.get("as_of"), "as_of")
    if expected_as_of is not None and as_of != _iso_time(expected_as_of, "expected_as_of"):
        raise MarketStructureStateError("market_structure_state.as_of must match the authoritative closed bar")
    session_key = _text(value.get("session_key"), "session_key", 80)
    primary = _pivots(value.get("primary_pivots"), "primary_pivots", PRIMARY_PIVOT_LIMIT, as_of)
    secondary = _pivots(value.get("secondary_pivots"), "secondary_pivots", SECONDARY_PIVOT_LIMIT, as_of)
    all_ids = [item["pivot_id"] for item in primary + secondary]
    if len(set(all_ids)) != len(all_ids):
        raise MarketStructureStateError("pivot ids must be unique across both levels")

    defenses = _defense_lines(value.get("defense_lines"), as_of)
    boundaries = _boundaries(value.get("provisional_invalidation_boundaries"), as_of)
    primary_ids = {item["pivot_id"] for item in primary}
    secondary_ids = {item["pivot_id"] for item in secondary}
    for slot, defense in defenses.items():
        if defense is None:
            continue
        expected_direction = "BULL" if slot.endswith("bull") else "BEAR"
        if defense["direction"] != expected_direction:
            raise MarketStructureStateError(f"{slot} direction does not match its slot")
        retained_ids = primary_ids if slot.startswith("small") else secondary_ids
        if defense["pivot_id"] not in retained_ids:
            raise MarketStructureStateError(f"{slot} must reference a retained pivot at the same level")
    for slot, boundary in boundaries.items():
        if boundary is not None and boundary["direction"] != slot.upper():
            raise MarketStructureStateError(f"{slot} provisional boundary direction does not match its slot")

    notes = value.get("notes")
    if not isinstance(notes, list) or not 1 <= len(notes) <= 5:
        raise MarketStructureStateError("market_structure_state.notes must contain one to five items")
    cleaned_notes = [_text(item, "notes item", 240) for item in notes]
    result = {
        "version": version,
        "as_of": as_of,
        "session_key": session_key,
        "primary_pivots": primary,
        "secondary_pivots": secondary,
        "defense_lines": defenses,
        "provisional_invalidation_boundaries": boundaries,
        "dow_state_small": _enum(value.get("dow_state_small"), DOW_STATES, "dow_state_small"),
        "dow_state_large": _enum(value.get("dow_state_large"), DOW_STATES, "dow_state_large"),
        "reversal_type": _enum(value.get("reversal_type"), REVERSAL_TYPES, "reversal_type"),
        "maturity": _enum(value.get("maturity"), MATURITY_STATES, "maturity"),
        "scenario_context": _scenario_context(value.get("scenario_context"), as_of),
        "notes": cleaned_notes,
    }
    if version >= ANCHOR_STATE_VERSION:
        try:
            result["anchor_context"] = validate_anchor_context(value.get("anchor_context"), as_of=as_of)
        except AnchorStateError as exc:
            raise MarketStructureStateError(str(exc)) from exc
    if version >= CCLASS_STATE_VERSION:
        try:
            result["cclass_context"] = validate_cclass_context(value.get("cclass_context"), as_of=as_of)
        except CClassStateError as exc:
            raise MarketStructureStateError(str(exc)) from exc
    if version >= DECISION_CHAIN_STATE_VERSION:
        try:
            result["decision_chain_context"] = validate_decision_chain_context(
                value.get("decision_chain_context"), as_of=as_of
            )
        except DecisionChainStateError as exc:
            raise MarketStructureStateError(str(exc)) from exc
        decision = result["decision_chain_context"]
        mapped_pattern = decision["opportunity_context"]["mapped_pattern"]
        setup_pattern = result["scenario_context"]["setup"]["pattern"]
        if mapped_pattern != "NONE" and mapped_pattern != setup_pattern:
            raise MarketStructureStateError("X decision chain must map to the existing scenario setup")
        lens = decision["structure_context"]["analysis_lens"]
        engine = result["cclass_context"]["engine_mode"]
        if lens == "YIZHI_OVERRIDE" and engine != "YIZHI_MOMENTUM":
            raise MarketStructureStateError("YIZHI_OVERRIDE requires the existing YIZHI engine mode")
        if lens in {"TAIJI_PRIMARY", "COMBINED_CONFIRMATION"} and engine != "TAIJI_ORDERED":
            raise MarketStructureStateError("Taiji decision lens requires the existing TAIJI engine mode")
        if lens in {"TAIJI_PRIMARY", "COMBINED_CONFIRMATION"}:
            retained_legs = len(result["cclass_context"]["taiji_context"]["legs"])
            if decision["structure_context"]["confirmed_leg_count"] > retained_legs:
                raise MarketStructureStateError("X decision chain cannot invent Taiji legs")
        setup_stage = result["scenario_context"]["setup"]["stage"]
        course_grade = decision["opportunity_context"]["course_grade"]
        stale_terminal_method = setup_stage in TERMINAL_SETUP_STAGES and course_grade == "OBSERVE"
        if mapped_pattern in TAIJI_METHODS and engine != "TAIJI_ORDERED":
            if stale_terminal_method:
                decision["opportunity_context"]["mapped_pattern"] = "NONE"
                mapped_pattern = "NONE"
            else:
                raise MarketStructureStateError("a Taiji strategy method requires the ordered Taiji engine")
        if mapped_pattern in YIZHI_METHODS and engine != "YIZHI_MOMENTUM":
            if stale_terminal_method:
                decision["opportunity_context"]["mapped_pattern"] = "NONE"
                mapped_pattern = "NONE"
            else:
                raise MarketStructureStateError("a Yizhi strategy method requires the momentum engine")
        if mapped_pattern in QUADRANT_METHODS:
            expected_quadrant = {
                "QUADRANT_Q1_MOMENTUM_BREAKOUT": "Q1",
                "QUADRANT_Q2_COUNTER_PULLBACK": "Q2",
                "QUADRANT_Q3_QUALIFIED_BOX": "Q3",
                "QUADRANT_Q4_TREND_ZONE": "Q4",
            }[mapped_pattern]
            if result["anchor_context"]["working_quadrant"] != expected_quadrant:
                raise MarketStructureStateError("a quadrant strategy method must match the working quadrant")
    if version == STATE_VERSION:
        try:
            result["prospective_context"] = validate_prospective_context(
                value.get("prospective_context"), as_of=as_of
            )
            validate_taiji_evolution_alignment(
                result["prospective_context"], result["cclass_context"]
            )
            _validate_prospective_setup_alignment(result)
        except ProspectiveStateError as exc:
            raise MarketStructureStateError(str(exc)) from exc
    return result


def validate_market_structure_semantics(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate Dow assertions that span pivots, defenses, and trend state.

    Older persisted snapshots remain readable through
    ``validate_market_structure_state``. Newly produced analyses use this
    stricter check so they cannot claim a confirmed Dow direction while
    omitting the paired attack pivot and active defense that define it.
    """

    state = validate_market_structure_state(value)
    if state["version"] >= ANCHOR_STATE_VERSION:
        try:
            state["anchor_context"] = validate_anchor_hierarchy_semantics(
                state["anchor_context"], as_of=state["as_of"]
            )
        except AnchorStateError as exc:
            raise MarketStructureStateError(str(exc)) from exc
    pivots_by_level = {
        "small": {item["pivot_id"]: item for item in state["primary_pivots"]},
        "large": {item["pivot_id"]: item for item in state["secondary_pivots"]},
    }
    expected_kind = {"BULL": "LOW", "BEAR": "HIGH"}

    for slot, defense in state["defense_lines"].items():
        if defense is None:
            continue
        level = "small" if slot.startswith("small") else "large"
        pivot = pivots_by_level[level][defense["pivot_id"]]
        direction = defense["direction"]
        if pivot["kind"] != expected_kind[direction]:
            raise MarketStructureStateError(
                f"{slot} must reference a {expected_kind[direction].lower()} attack pivot"
            )
        if pivot["state"] != "PAIRED_CONFIRMED":
            raise MarketStructureStateError(f"{slot} must reference a paired-confirmed pivot")
        if (
            defense["price_estimate"] is not None
            and pivot["price_estimate"] is not None
            and defense["price_estimate"] != pivot["price_estimate"]
        ):
            raise MarketStructureStateError(f"{slot} price must match its source pivot")

    for level, dow_key in (("small", "dow_state_small"), ("large", "dow_state_large")):
        direction = state[dow_key]
        if direction not in {"BULL", "BEAR"}:
            continue
        slot = f"{level}_{direction.lower()}"
        defense = state["defense_lines"][slot]
        if defense is None or defense["status"] != "ACTIVE":
            raise MarketStructureStateError(
                f"{dow_key}={direction} requires an active same-grade {direction.lower()} defense"
            )

    return state


def _validate_prospective_setup_alignment(state: Mapping[str, Any]) -> None:
    setup = state["scenario_context"]["setup"]
    prospective = state["prospective_context"]
    armed = [
        item
        for item in (prospective["long_playbook"], prospective["short_playbook"])
        if item["status"] == "ARMED"
    ]
    if len(armed) > 1:
        raise ProspectiveStateError("only one directional playbook may be armed at a time")
    if not armed:
        return
    playbook = armed[0]
    if setup["stage"] == "NONE":
        raise ProspectiveStateError("an armed playbook requires an active scenario setup")
    if playbook["mapped_pattern"] != setup["pattern"]:
        raise ProspectiveStateError("the armed playbook strategy must match the scenario setup")
    if playbook["direction"] != setup["direction"]:
        raise ProspectiveStateError("the armed playbook direction must match the scenario setup")


def upgrade_market_structure_state(
    value: Mapping[str, Any],
    *,
    target_version: int,
) -> dict[str, Any]:
    current = validate_market_structure_state(value)
    if current["version"] == target_version:
        return current
    if current["version"] == PREVIOUS_STATE_VERSION and target_version in {
        ANCHOR_STATE_VERSION,
        CCLASS_STATE_VERSION,
        DECISION_CHAIN_STATE_VERSION,
        STATE_VERSION,
    }:
        current = {**current, "version": ANCHOR_STATE_VERSION, "anchor_context": empty_anchor_context()}
        current = validate_market_structure_state(current)
    if current["version"] == ANCHOR_STATE_VERSION and target_version in {
        CCLASS_STATE_VERSION,
        DECISION_CHAIN_STATE_VERSION,
        STATE_VERSION,
    }:
        upgraded = {
            **current,
            "version": CCLASS_STATE_VERSION,
            "cclass_context": empty_cclass_context(as_of=current["as_of"]),
        }
        current = validate_market_structure_state(upgraded)
    if current["version"] == CCLASS_STATE_VERSION and target_version in {
        DECISION_CHAIN_STATE_VERSION,
        STATE_VERSION,
    }:
        upgraded = {
            **current,
            "version": DECISION_CHAIN_STATE_VERSION,
            "decision_chain_context": empty_decision_chain_context(as_of=current["as_of"]),
        }
        current = validate_market_structure_state(upgraded)
    if current["version"] == DECISION_CHAIN_STATE_VERSION and target_version == STATE_VERSION:
        upgraded = {
            **current,
            "version": STATE_VERSION,
            "prospective_context": empty_prospective_context(as_of=current["as_of"]),
        }
        return validate_market_structure_state(upgraded)
    if current["version"] == target_version:
        return current
    raise MarketStructureStateError("unsupported market structure state version transition")


def validate_market_structure_transition(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    *,
    expected_as_of: str,
) -> dict[str, Any]:
    next_state = validate_market_structure_state(current, expected_as_of=expected_as_of)
    if previous is None:
        return next_state
    prior = validate_market_structure_state(previous)
    if prior["version"] != next_state["version"]:
        try:
            prior = upgrade_market_structure_state(prior, target_version=next_state["version"])
        except MarketStructureStateError as exc:
            raise MarketStructureStateError("market structure state versions are incompatible") from exc
    prior_time = _parse_time(prior["as_of"], "previous.as_of")
    next_time = _parse_time(next_state["as_of"], "current.as_of")
    if next_time < prior_time:
        raise MarketStructureStateError("market structure state cannot move backward in time")
    if next_time == prior_time:
        if next_state != prior:
            raise MarketStructureStateError("same-bar market structure retries must be identical")
        return next_state
    if next_state["session_key"] != prior["session_key"]:
        return next_state

    _normalize_mechanical_transition_times(prior, next_state)

    referenced = {item["pivot_id"] for item in next_state["defense_lines"].values() if item is not None}
    _validate_pivot_transitions(prior["primary_pivots"], next_state["primary_pivots"], prior_time, referenced)
    _validate_pivot_transitions(prior["secondary_pivots"], next_state["secondary_pivots"], prior_time, referenced)
    for slot in DEFENSE_SLOTS:
        _validate_defense_transition(prior["defense_lines"][slot], next_state["defense_lines"][slot], f"defense_lines.{slot}")
    for slot in ("bull", "bear"):
        _validate_boundary_transition(
            prior["provisional_invalidation_boundaries"][slot],
            next_state["provisional_invalidation_boundaries"][slot],
            f"provisional_invalidation_boundaries.{slot}",
        )
    _validate_scenario_transition(prior["scenario_context"], next_state["scenario_context"], prior_time)
    if next_state["version"] >= ANCHOR_STATE_VERSION:
        try:
            validate_anchor_transition(
                prior["anchor_context"],
                next_state["anchor_context"],
                prior_as_of=prior["as_of"],
                current_as_of=next_state["as_of"],
            )
        except AnchorStateError as exc:
            raise MarketStructureStateError(str(exc)) from exc
    if next_state["version"] >= CCLASS_STATE_VERSION:
        try:
            validate_cclass_transition(
                prior["cclass_context"],
                next_state["cclass_context"],
                prior_as_of=prior["as_of"],
                current_as_of=next_state["as_of"],
                allow_taiji_lineage_replacement=_is_explicit_taiji_lineage_replacement(
                    prior, next_state
                ),
            )
        except CClassStateError as exc:
            raise MarketStructureStateError(str(exc)) from exc
    if next_state["version"] >= DECISION_CHAIN_STATE_VERSION:
        try:
            validate_decision_chain_transition(
                prior["decision_chain_context"],
                next_state["decision_chain_context"],
                prior_as_of=prior["as_of"],
                current_as_of=next_state["as_of"],
            )
        except DecisionChainStateError as exc:
            raise MarketStructureStateError(str(exc)) from exc
    if next_state["version"] == STATE_VERSION:
        try:
            validate_prospective_transition(
                prior["prospective_context"],
                next_state["prospective_context"],
                prior_as_of=prior["as_of"],
                current_as_of=next_state["as_of"],
            )
        except ProspectiveStateError as exc:
            raise MarketStructureStateError(str(exc)) from exc
    return next_state


def _is_explicit_taiji_lineage_replacement(
    prior: Mapping[str, Any], current: Mapping[str, Any]
) -> bool:
    """Allow a clean Taiji rebuild only for an explicit large-anchor repair.

    This is deliberately narrower than a generic lifecycle exception.  The old
    active large anchor must be retained as REPLACED and point to a causal repair.
    A same-cycle repair followed by an opposite active anchor is accepted only when
    the repaired anchor and its handoff extreme are explicit.
    """

    old_anchors = prior.get("anchor_context")
    new_anchors = current.get("anchor_context")
    old_cclass = prior.get("cclass_context")
    new_cclass = current.get("cclass_context")
    if not all(isinstance(item, Mapping) for item in (old_anchors, new_anchors, old_cclass, new_cclass)):
        return False

    old_large_id = old_anchors.get("active_large_anchor_id")
    new_large_id = new_anchors.get("active_large_anchor_id")
    if not old_large_id or not new_large_id or old_large_id == new_large_id:
        return False

    retained_old = next(
        (item for item in new_anchors.get("anchors", []) if item.get("anchor_id") == old_large_id),
        None,
    )
    active_new = next(
        (item for item in new_anchors.get("anchors", []) if item.get("anchor_id") == new_large_id),
        None,
    )
    if not isinstance(retained_old, Mapping) or not isinstance(active_new, Mapping):
        return False
    if retained_old.get("status") != "REPLACED" or not retained_old.get("replaced_by"):
        return False
    if active_new.get("grade") != "LARGE" or active_new.get("status") not in {"FORMING", "CONFIRMED"}:
        return False

    replacement_id = retained_old.get("replaced_by")
    replacement = next(
        (item for item in new_anchors.get("anchors", []) if item.get("anchor_id") == replacement_id),
        None,
    )
    if not isinstance(replacement, Mapping) or replacement.get("grade") != "LARGE":
        return False

    direct_replacement = replacement_id == new_large_id
    if not direct_replacement:
        old_anchor = next(
            (item for item in old_anchors.get("anchors", []) if item.get("anchor_id") == old_large_id),
            None,
        )
        if not isinstance(old_anchor, Mapping):
            return False
        repair_then_flip = bool(
            replacement.get("first_seen_at") == current.get("as_of")
            and replacement.get("status") in {"INVALIDATED", "REPLACED"}
            and replacement.get("direction") == old_anchor.get("direction")
            and replacement.get("start_bar_time") <= old_anchor.get("start_bar_time")
            and active_new.get("first_seen_at") == current.get("as_of")
            and active_new.get("direction") != replacement.get("direction")
            and active_new.get("start_bar_time") == replacement.get("extreme_bar_time")
        )
        if not repair_then_flip:
            return False

    old_taiji = old_cclass.get("taiji_context")
    new_taiji = new_cclass.get("taiji_context")
    if not isinstance(old_taiji, Mapping) or not isinstance(new_taiji, Mapping):
        return False
    return bool(
        old_taiji.get("active_leg_id")
        and new_taiji.get("active_leg_id")
        and old_taiji.get("anchor_id") == old_large_id
        and new_taiji.get("anchor_id") == new_large_id
        and old_taiji.get("dynasty_id")
        and new_taiji.get("dynasty_id")
        and old_taiji.get("dynasty_id") != new_taiji.get("dynasty_id")
    )


def _normalize_mechanical_transition_times(
    prior: Mapping[str, Any],
    current: dict[str, Any],
) -> None:
    """Repair bookkeeping timestamps without changing any market classification.

    The model chooses the structure, setup, mode, lens, and assessment content.  The
    timestamp attached to an already-chosen state transition is mechanical: it is
    either the current authoritative closed-bar time, the previous timestamp when
    nothing changed, or ``None`` for an explicitly empty/undefined state.  Keeping
    that bookkeeping deterministic prevents a valid chart analysis from being
    discarded solely because the model copied a stale ``*_changed_at`` value.
    """

    as_of = current["as_of"]

    old_setup = prior["scenario_context"]["setup"]
    new_setup = current["scenario_context"]["setup"]
    if old_setup["setup_id"] == new_setup["setup_id"] and new_setup["stage"] != "NONE":
        new_setup["stage_changed_at"] = (
            as_of if old_setup["stage"] != new_setup["stage"] else old_setup["stage_changed_at"]
        )

    if current["version"] >= ANCHOR_STATE_VERSION:
        old_anchor = prior["anchor_context"]
        new_anchor = current["anchor_context"]

        # Terminal anchors are historical facts.  A later model response may
        # paraphrase their explanatory notes while carrying the anchor forward,
        # but that wording change must not make an otherwise valid new bar fail
        # the immutable-transition contract.  Preserve the previously committed
        # object verbatim; active/forming anchors remain model-owned and continue
        # through the ordinary transition checks below.
        old_anchors_by_id = {item["anchor_id"]: item for item in old_anchor["anchors"]}
        new_anchor["anchors"] = [
            deepcopy(old_anchors_by_id[item["anchor_id"]])
            if (
                item["anchor_id"] in old_anchors_by_id
                and old_anchors_by_id[item["anchor_id"]]["status"] in {"INVALIDATED", "REPLACED"}
            )
            else item
            for item in new_anchor["anchors"]
        ]
        old_control = (old_anchor["controlling_grade"], old_anchor["grade_relation"])
        new_control = (new_anchor["controlling_grade"], new_anchor["grade_relation"])
        if old_control == new_control:
            new_anchor["control_changed_at"] = old_anchor["control_changed_at"]
        elif (
            new_anchor["controlling_grade"] == "UNDEFINED"
            and new_anchor["active_large_anchor_id"] is None
            and new_anchor["active_small_anchor_id"] is None
        ):
            new_anchor["control_changed_at"] = None
        else:
            new_anchor["control_changed_at"] = as_of

        old_quadrant = (
            old_anchor["working_quadrant"],
            old_anchor["primary_quadrant_candidate"],
            old_anchor["secondary_quadrant_candidate"],
            tuple(old_anchor["eliminated_quadrants"]),
        )
        new_quadrant = (
            new_anchor["working_quadrant"],
            new_anchor["primary_quadrant_candidate"],
            new_anchor["secondary_quadrant_candidate"],
            tuple(new_anchor["eliminated_quadrants"]),
        )
        if old_quadrant == new_quadrant:
            new_anchor["quadrant_changed_at"] = old_anchor["quadrant_changed_at"]
        elif (
            new_anchor["working_quadrant"] == "UNDEFINED"
            and new_anchor["primary_quadrant_candidate"] == "UNDEFINED"
            and new_anchor["secondary_quadrant_candidate"] is None
            and not new_anchor["eliminated_quadrants"]
        ):
            new_anchor["quadrant_changed_at"] = None
        else:
            new_anchor["quadrant_changed_at"] = as_of

    if current["version"] >= CCLASS_STATE_VERSION:
        old_cclass = prior["cclass_context"]
        new_cclass = current["cclass_context"]
        old_engine = (old_cclass["engine_mode"], old_cclass["order_state"])
        new_engine = (new_cclass["engine_mode"], new_cclass["order_state"])
        if old_engine == new_engine:
            new_cclass["engine_changed_at"] = old_cclass["engine_changed_at"]
        elif new_cclass["engine_mode"] == "UNDEFINED":
            new_cclass["engine_changed_at"] = None
        else:
            new_cclass["engine_changed_at"] = as_of

        old_taiji = old_cclass["taiji_context"]
        new_taiji = new_cclass["taiji_context"]
        old_active = {item["leg_id"]: item for item in old_taiji["legs"]}.get(old_taiji["active_leg_id"])
        new_active = {item["leg_id"]: item for item in new_taiji["legs"]}.get(new_taiji["active_leg_id"])
        taiji_quality = (
            "status",
            "copy_quality",
            "correction_quality",
            "amplitude_relation",
            "duration_relation",
            "slope_relation",
            "cleanliness_relation",
            "destructive_relation",
        )
        taiji_changed = (
            old_taiji["active_leg_id"] != new_taiji["active_leg_id"]
            or old_taiji["anchor_time_status"] != new_taiji["anchor_time_status"]
            or old_taiji["previous_context_alignment"] != new_taiji["previous_context_alignment"]
            or (
                old_active is not None
                and new_active is not None
                and any(old_active[key] != new_active[key] for key in taiji_quality)
            )
        )
        if not taiji_changed:
            new_taiji["assessment_changed_at"] = old_taiji["assessment_changed_at"]
        elif new_taiji["active_leg_id"] is None:
            new_taiji["assessment_changed_at"] = None
        else:
            new_taiji["assessment_changed_at"] = as_of

        old_momentum = old_cclass["momentum_context"]
        new_momentum = new_cclass["momentum_context"]
        momentum_changed = any(
            old_momentum[key] != new_momentum[key]
            for key in ("stage", "quality", "dragon_grade", "distance_phase", "location", "mapped_pattern")
        )
        if not momentum_changed:
            new_momentum["stage_changed_at"] = old_momentum["stage_changed_at"]
        elif new_momentum["stage"] == "NONE":
            new_momentum["stage_changed_at"] = None
        else:
            new_momentum["stage_changed_at"] = as_of

        old_thesis = old_cclass["thesis_context"]
        new_thesis = new_cclass["thesis_context"]
        if old_thesis["bias"] == new_thesis["bias"]:
            new_thesis["changed_at"] = old_thesis["changed_at"]
        elif new_thesis["bias"] == "UNDEFINED":
            new_thesis["changed_at"] = None
        else:
            new_thesis["changed_at"] = as_of

    if current["version"] >= DECISION_CHAIN_STATE_VERSION:
        old_decision = prior["decision_chain_context"]
        new_decision = current["decision_chain_context"]
        if old_decision["process_stage"] == new_decision["process_stage"]:
            new_decision["stage_changed_at"] = old_decision["stage_changed_at"]
        elif new_decision["process_stage"] == "UNDEFINED":
            new_decision["stage_changed_at"] = None
        else:
            new_decision["stage_changed_at"] = as_of

        old_lens = old_decision["structure_context"]["analysis_lens"]
        new_lens = new_decision["structure_context"]["analysis_lens"]
        if old_lens == new_lens:
            new_decision["structure_context"]["lens_changed_at"] = old_decision["structure_context"][
                "lens_changed_at"
            ]
        elif new_lens == "UNDEFINED":
            new_decision["structure_context"]["lens_changed_at"] = None
        else:
            new_decision["structure_context"]["lens_changed_at"] = as_of

        old_assessment = {
            key: value
            for key, value in old_decision.items()
            if key not in {"as_of", "assessment_changed_at"}
        }
        new_assessment = {
            key: value
            for key, value in new_decision.items()
            if key not in {"as_of", "assessment_changed_at"}
        }
        if old_assessment == new_assessment:
            new_decision["assessment_changed_at"] = old_decision["assessment_changed_at"]
        elif new_decision["process_stage"] == "UNDEFINED":
            new_decision["assessment_changed_at"] = None
        else:
            new_decision["assessment_changed_at"] = as_of

    if current["version"] == STATE_VERSION:
        old_prospective = prior["prospective_context"]
        new_prospective = current["prospective_context"]
        old_signature = (
            old_prospective["market_bias"],
            old_prospective["primary_hypothesis"]["hypothesis_id"],
            old_prospective["primary_hypothesis"]["status"],
            old_prospective["primary_hypothesis"]["confidence"],
            old_prospective["alternative_hypothesis"]["hypothesis_id"],
            old_prospective["alternative_hypothesis"]["status"],
            old_prospective["alternative_hypothesis"]["confidence"],
        )
        new_signature = (
            new_prospective["market_bias"],
            new_prospective["primary_hypothesis"]["hypothesis_id"],
            new_prospective["primary_hypothesis"]["status"],
            new_prospective["primary_hypothesis"]["confidence"],
            new_prospective["alternative_hypothesis"]["hypothesis_id"],
            new_prospective["alternative_hypothesis"]["status"],
            new_prospective["alternative_hypothesis"]["confidence"],
        )
        if old_signature == new_signature:
            new_prospective["assessment_changed_at"] = old_prospective["assessment_changed_at"]
        elif new_prospective["market_bias"] == "UNDEFINED":
            new_prospective["assessment_changed_at"] = None
        else:
            new_prospective["assessment_changed_at"] = as_of
        old_by_id = {
            item["hypothesis_id"]: item
            for item in (
                old_prospective["primary_hypothesis"],
                old_prospective["alternative_hypothesis"],
            )
            if item["hypothesis_id"] is not None
        }
        for item in (
            new_prospective["primary_hypothesis"],
            new_prospective["alternative_hypothesis"],
        ):
            old_item = old_by_id.get(item["hypothesis_id"])
            if old_item is None and item["hypothesis_id"] is not None:
                # A newly minted hypothesis id cannot inherit the first-seen
                # timestamp of a previous hypothesis.  The id choice remains
                # model-owned; its causal bookkeeping begins at this snapshot.
                item["first_seen_at"] = as_of
                item["status_changed_at"] = as_of
            elif old_item is not None:
                item["status_changed_at"] = (
                    as_of if old_item["status"] != item["status"] else old_item["status_changed_at"]
                )


def market_structure_notification_reasons(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> list[str]:
    """Return material structure/scenario changes that must not be hidden by DONT_NOTIFY."""
    next_state = validate_market_structure_state(current)
    if previous is None:
        return ["結構狀態首次建立"]
    prior = validate_market_structure_state(previous)
    if prior["version"] != next_state["version"]:
        try:
            prior = upgrade_market_structure_state(prior, target_version=next_state["version"])
        except MarketStructureStateError as exc:
            raise MarketStructureStateError("market structure state versions are incompatible") from exc
    if prior["session_key"] != next_state["session_key"]:
        return ["交易時段結構重設"]
    reasons: list[str] = []
    if prior["defense_lines"] != next_state["defense_lines"]:
        reasons.append("多空道氏防線更新")
    if prior["provisional_invalidation_boundaries"] != next_state["provisional_invalidation_boundaries"]:
        reasons.append("暫代失效邊界更新")
    for key, label in (
        ("dow_state_small", "小級道氏狀態改變"),
        ("dow_state_large", "大級道氏狀態改變"),
        ("reversal_type", "道氏反轉型態改變"),
        ("maturity", "左右成熟度改變"),
    ):
        if prior[key] != next_state[key]:
            reasons.append(label)
    before_scenario = prior["scenario_context"]
    after_scenario = next_state["scenario_context"]
    for key, label in (
        ("wave_phase", "波段階段改變"),
        ("locations", "所在位置改變"),
        ("grade_alignment", "級數一致性改變"),
    ):
        if before_scenario[key] != after_scenario[key]:
            reasons.append(label)
    before_setup = before_scenario["setup"]
    after_setup = after_scenario["setup"]
    if any(before_setup[key] != after_setup[key] for key in ("setup_id", "pattern", "direction", "stage")):
        reasons.append("候選型態階段改變")
    elif before_setup["stage"] not in {"NONE", "INVALIDATED", "NO_CHASE"} and any(
        before_setup[key] != after_setup[key]
        for key in ("observation_zone", "trigger_zone", "invalidation_zone", "nearest_obstacle_zone")
    ):
        reasons.append("候選點位區域更新")
    if next_state["version"] >= ANCHOR_STATE_VERSION:
        reasons.extend(anchor_notification_reasons(prior["anchor_context"], next_state["anchor_context"]))
    if next_state["version"] >= CCLASS_STATE_VERSION:
        reasons.extend(cclass_notification_reasons(prior["cclass_context"], next_state["cclass_context"]))
    if next_state["version"] >= DECISION_CHAIN_STATE_VERSION:
        reasons.extend(
            decision_chain_notification_reasons(
                prior["decision_chain_context"], next_state["decision_chain_context"]
            )
        )
    if next_state["version"] == STATE_VERSION:
        reasons.extend(
            prospective_notification_reasons(
                prior["prospective_context"], next_state["prospective_context"]
            )
        )
    return reasons


def _pivots(value: Any, field: str, limit: int, as_of: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > limit:
        raise MarketStructureStateError(f"{field} must be a list with at most {limit} items")
    result = [_pivot(item, f"{field} item", as_of) for item in value]
    times = [_parse_time(item["bar_time"], f"{field}.bar_time") for item in result]
    if times != sorted(times):
        raise MarketStructureStateError(f"{field} must be ordered by pivot bar time")
    return result


def _pivot(value: Any, field: str, as_of: str) -> dict[str, Any]:
    keys = {
        "pivot_id", "kind", "state", "bar_time", "price_estimate", "first_seen_at",
        "locally_confirmed_at", "paired_confirmed_at", "terminal_at", "replaced_by", "proportionality",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise MarketStructureStateError(f"{field} has invalid fields")
    state = _enum(value.get("state"), PIVOT_STATES, f"{field}.state")
    bar_time = _iso_time(value.get("bar_time"), f"{field}.bar_time")
    first_seen = _iso_time(value.get("first_seen_at"), f"{field}.first_seen_at")
    local = _optional_time(value.get("locally_confirmed_at"), f"{field}.locally_confirmed_at")
    paired = _optional_time(value.get("paired_confirmed_at"), f"{field}.paired_confirmed_at")
    terminal = _optional_time(value.get("terminal_at"), f"{field}.terminal_at")
    ceiling = _parse_time(as_of, "as_of")
    ordered_times = [_parse_time(bar_time, "bar_time"), _parse_time(first_seen, "first_seen_at")]
    ordered_times += [_parse_time(item, "pivot transition time") for item in (local, paired, terminal) if item]
    if any(item > ceiling for item in ordered_times):
        raise MarketStructureStateError(f"{field} contains a future timestamp")
    if local and _parse_time(local, "locally_confirmed_at") < _parse_time(first_seen, "first_seen_at"):
        raise MarketStructureStateError(f"{field} local confirmation predates first sighting")
    if paired and (not local or _parse_time(paired, "paired_confirmed_at") < _parse_time(local, "locally_confirmed_at")):
        raise MarketStructureStateError(f"{field} paired confirmation requires an earlier local confirmation")
    if state == "CANDIDATE" and (local or paired or terminal):
        raise MarketStructureStateError(f"{field} candidate has impossible transition timestamps")
    if state == "LOCAL_CONFIRMED" and (not local or paired or terminal):
        raise MarketStructureStateError(f"{field} local confirmation timestamps are invalid")
    if state == "PAIRED_CONFIRMED" and (not local or not paired or terminal):
        raise MarketStructureStateError(f"{field} paired confirmation timestamps are invalid")
    if state in TERMINAL_PIVOT_STATES and terminal is None:
        raise MarketStructureStateError(f"{field} terminal state requires terminal_at")
    replaced_by = value.get("replaced_by")
    if state == "REPLACED":
        replaced_by = _text(replaced_by, f"{field}.replaced_by", 120)
    elif replaced_by is not None:
        raise MarketStructureStateError(f"{field}.replaced_by is only valid for REPLACED")
    return {
        "pivot_id": _text(value.get("pivot_id"), f"{field}.pivot_id", 120),
        "kind": _enum(value.get("kind"), PIVOT_KINDS, f"{field}.kind"),
        "state": state,
        "bar_time": bar_time,
        "price_estimate": _optional_positive_number(value.get("price_estimate"), f"{field}.price_estimate"),
        "first_seen_at": first_seen,
        "locally_confirmed_at": local,
        "paired_confirmed_at": paired,
        "terminal_at": terminal,
        "replaced_by": replaced_by,
        "proportionality": _enum(value.get("proportionality"), PROPORTIONALITY, f"{field}.proportionality"),
    }


def _defense(value: Any, field: str, as_of: str) -> dict[str, Any] | None:
    if value is None:
        return None
    keys = {"direction", "pivot_id", "price_estimate", "qualified_at", "status", "ended_at"}
    if not isinstance(value, Mapping) or set(value) != keys:
        raise MarketStructureStateError(f"{field} has invalid fields")
    status = _enum(value.get("status"), DEFENSE_STATUS, f"{field}.status")
    ended = _optional_time(value.get("ended_at"), f"{field}.ended_at")
    if (status == "ACTIVE") != (ended is None):
        raise MarketStructureStateError(f"{field}.ended_at does not match its status")
    qualified = _iso_time(value.get("qualified_at"), f"{field}.qualified_at")
    if _parse_time(qualified, "qualified_at") > _parse_time(as_of, "as_of"):
        raise MarketStructureStateError(f"{field} is qualified in the future")
    return {
        "direction": _enum(value.get("direction"), {"BULL", "BEAR"}, f"{field}.direction"),
        "pivot_id": _text(value.get("pivot_id"), f"{field}.pivot_id", 120),
        "price_estimate": _optional_positive_number(value.get("price_estimate"), f"{field}.price_estimate"),
        "qualified_at": qualified,
        "status": status,
        "ended_at": ended,
    }


def _defense_lines(value: Any, as_of: str) -> dict[str, dict[str, Any] | None]:
    if not isinstance(value, Mapping) or set(value) != set(DEFENSE_SLOTS):
        raise MarketStructureStateError("defense_lines has invalid fields")
    return {slot: _defense(value.get(slot), f"defense_lines.{slot}", as_of) for slot in DEFENSE_SLOTS}


def _boundary(value: Any, as_of: str) -> dict[str, Any] | None:
    if value is None:
        return None
    keys = {"kind", "direction", "price_estimate", "established_at", "status", "ended_at"}
    if not isinstance(value, Mapping) or set(value) != keys:
        raise MarketStructureStateError("provisional_invalidation_boundary has invalid fields")
    status = _enum(value.get("status"), DEFENSE_STATUS, "boundary.status")
    ended = _optional_time(value.get("ended_at"), "boundary.ended_at")
    if (status == "ACTIVE") != (ended is None):
        raise MarketStructureStateError("boundary.ended_at does not match its status")
    established = _iso_time(value.get("established_at"), "boundary.established_at")
    if _parse_time(established, "established_at") > _parse_time(as_of, "as_of"):
        raise MarketStructureStateError("boundary is established in the future")
    return {
        "kind": _enum(value.get("kind"), BOUNDARY_KINDS, "boundary.kind"),
        "direction": _enum(value.get("direction"), {"BULL", "BEAR"}, "boundary.direction"),
        "price_estimate": _optional_positive_number(value.get("price_estimate"), "boundary.price_estimate"),
        "established_at": established,
        "status": status,
        "ended_at": ended,
    }


def _boundaries(value: Any, as_of: str) -> dict[str, dict[str, Any] | None]:
    if not isinstance(value, Mapping) or set(value) != {"bull", "bear"}:
        raise MarketStructureStateError("provisional_invalidation_boundaries has invalid fields")
    return {
        slot: _boundary(value.get(slot), as_of)
        for slot in ("bull", "bear")
    }


def _empty_zone(reason: str = "圖面不足，暫不提供可靠區間。") -> dict[str, Any]:
    return {"low": None, "high": None, "reliability": "UNAVAILABLE", "reason": reason}


def _empty_scenario_context() -> dict[str, Any]:
    return {
        "wave_phase": "UNDEFINED",
        "locations": ["UNDEFINED"],
        "grade_alignment": "UNDEFINED",
        "hold_scenario": "尚無可驗證的守住情境。",
        "break_scenario": "尚無可驗證的突破或跌破情境。",
        "setup": {
            "setup_id": None,
            "pattern": "NONE",
            "direction": None,
            "stage": "NONE",
            "first_seen_at": None,
            "stage_changed_at": None,
            "observation_zone": _empty_zone(),
            "trigger_zone": _empty_zone(),
            "invalidation_zone": _empty_zone(),
            "nearest_obstacle_zone": _empty_zone(),
        },
    }


def _scenario_context(value: Any, as_of: str) -> dict[str, Any]:
    keys = {"wave_phase", "locations", "grade_alignment", "hold_scenario", "break_scenario", "setup"}
    if not isinstance(value, Mapping) or set(value) != keys:
        raise MarketStructureStateError("scenario_context has invalid fields")
    locations = value.get("locations")
    if not isinstance(locations, list) or not 1 <= len(locations) <= 4:
        raise MarketStructureStateError("scenario_context.locations must contain one to four items")
    cleaned_locations = [_enum(item, LOCATIONS, "scenario_context.locations item") for item in locations]
    if len(set(cleaned_locations)) != len(cleaned_locations):
        raise MarketStructureStateError("scenario_context.locations must not contain duplicates")
    if "UNDEFINED" in cleaned_locations and len(cleaned_locations) != 1:
        raise MarketStructureStateError("UNDEFINED cannot be combined with other locations")
    return {
        "wave_phase": _enum(value.get("wave_phase"), WAVE_PHASES, "scenario_context.wave_phase"),
        "locations": cleaned_locations,
        "grade_alignment": _enum(value.get("grade_alignment"), GRADE_ALIGNMENT, "scenario_context.grade_alignment"),
        "hold_scenario": _text(value.get("hold_scenario"), "scenario_context.hold_scenario", 240),
        "break_scenario": _text(value.get("break_scenario"), "scenario_context.break_scenario", 240),
        "setup": _setup_context(value.get("setup"), as_of),
    }


def _setup_context(value: Any, as_of: str) -> dict[str, Any]:
    keys = {
        "setup_id", "pattern", "direction", "stage", "first_seen_at", "stage_changed_at",
        "observation_zone", "trigger_zone", "invalidation_zone", "nearest_obstacle_zone",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise MarketStructureStateError("scenario_context.setup has invalid fields")
    stage = _enum(value.get("stage"), SETUP_STAGES, "scenario_context.setup.stage")
    pattern = _enum(value.get("pattern"), PATTERN_TYPES, "scenario_context.setup.pattern")
    setup_id = value.get("setup_id")
    direction = value.get("direction")
    first_seen = _optional_time(value.get("first_seen_at"), "scenario_context.setup.first_seen_at")
    stage_changed = _optional_time(value.get("stage_changed_at"), "scenario_context.setup.stage_changed_at")
    if stage == "NONE":
        if pattern != "NONE" or any(item is not None for item in (setup_id, direction, first_seen, stage_changed)):
            raise MarketStructureStateError("inactive setup must use NONE and null identifiers")
    else:
        setup_id = _text(setup_id, "scenario_context.setup.setup_id", 120)
        direction = _enum(direction, {"LONG", "SHORT"}, "scenario_context.setup.direction")
        if pattern == "NONE" or first_seen is None or stage_changed is None:
            raise MarketStructureStateError("active setup requires pattern and causal timestamps")
        ceiling = _parse_time(as_of, "as_of")
        first_time = _parse_time(first_seen, "first_seen_at")
        changed_time = _parse_time(stage_changed, "stage_changed_at")
        if first_time > changed_time or changed_time > ceiling:
            raise MarketStructureStateError("setup timestamps are not causal")
    zones = {
        key: _price_zone(value.get(key), f"scenario_context.setup.{key}")
        for key in ("observation_zone", "trigger_zone", "invalidation_zone", "nearest_obstacle_zone")
    }
    if stage in {"AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED"}:
        if zones["trigger_zone"]["reliability"] == "UNAVAILABLE" or zones["invalidation_zone"]["reliability"] == "UNAVAILABLE":
            raise MarketStructureStateError("confirmed setup requires trigger and invalidation zones")
    return {
        "setup_id": setup_id,
        "pattern": pattern,
        "direction": direction,
        "stage": stage,
        "first_seen_at": first_seen,
        "stage_changed_at": stage_changed,
        **zones,
    }


def _price_zone(value: Any, field: str) -> dict[str, Any]:
    keys = {"low", "high", "reliability", "reason"}
    if not isinstance(value, Mapping) or set(value) != keys:
        raise MarketStructureStateError(f"{field} has invalid fields")
    reliability = _enum(value.get("reliability"), ZONE_RELIABILITY, f"{field}.reliability")
    low = _optional_positive_number(value.get("low"), f"{field}.low")
    high = _optional_positive_number(value.get("high"), f"{field}.high")
    if reliability == "UNAVAILABLE":
        if low is not None or high is not None:
            raise MarketStructureStateError(f"{field} unavailable zone must not contain prices")
    elif low is None or high is None or low > high:
        raise MarketStructureStateError(f"{field} available zone must contain an ordered range")
    return {
        "low": low,
        "high": high,
        "reliability": reliability,
        "reason": _text(value.get("reason"), f"{field}.reason", 160),
    }


def _migrate_legacy_state(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != LEGACY_TOP_LEVEL_KEYS:
        raise MarketStructureStateError("legacy market_structure_state has invalid top-level fields")
    defenses = {slot: None for slot in DEFENSE_SLOTS}
    for level, legacy_key in (("small", "small_defense_line"), ("large", "large_defense_line")):
        defense = value.get(legacy_key)
        if isinstance(defense, Mapping) and defense.get("direction") in {"BULL", "BEAR"}:
            defenses[f"{level}_{str(defense['direction']).lower()}"] = dict(defense)
    boundaries = {"bull": None, "bear": None}
    boundary = value.get("provisional_invalidation_boundary")
    if isinstance(boundary, Mapping) and boundary.get("direction") in {"BULL", "BEAR"}:
        boundaries[str(boundary["direction"]).lower()] = dict(boundary)
    return {
        "version": PREVIOUS_STATE_VERSION,
        "as_of": value.get("as_of"),
        "session_key": value.get("session_key"),
        "primary_pivots": value.get("primary_pivots"),
        "secondary_pivots": value.get("secondary_pivots"),
        "defense_lines": defenses,
        "provisional_invalidation_boundaries": boundaries,
        "dow_state_small": value.get("dow_state_small"),
        "dow_state_large": value.get("dow_state_large"),
        "reversal_type": value.get("reversal_type"),
        "maturity": value.get("maturity"),
        "scenario_context": _empty_scenario_context(),
        "notes": value.get("notes"),
    }


def _validate_pivot_transitions(
    previous: list[dict[str, Any]],
    current: list[dict[str, Any]],
    prior_as_of: datetime,
    referenced: set[str],
) -> None:
    old = {item["pivot_id"]: item for item in previous}
    new = {item["pivot_id"]: item for item in current}
    for pivot_id, before in old.items():
        after = new.get(pivot_id)
        if after is None:
            if before["state"] in {"CANDIDATE", "LOCAL_CONFIRMED", "PAIRED_CONFIRMED"} or pivot_id in referenced:
                raise MarketStructureStateError("active, paired, or referenced pivots cannot disappear")
            continue
        for immutable in ("pivot_id", "kind", "bar_time", "first_seen_at"):
            if before[immutable] != after[immutable]:
                raise MarketStructureStateError(f"pivot {pivot_id} changed immutable field {immutable}")
        allowed = {
            "CANDIDATE": {"CANDIDATE", "LOCAL_CONFIRMED", "INVALIDATED", "REPLACED"},
            "LOCAL_CONFIRMED": {"LOCAL_CONFIRMED", "PAIRED_CONFIRMED", "INVALIDATED", "REPLACED"},
            "PAIRED_CONFIRMED": {"PAIRED_CONFIRMED"},
            "INVALIDATED": {"INVALIDATED"},
            "REPLACED": {"REPLACED"},
        }
        if after["state"] not in allowed[before["state"]]:
            raise MarketStructureStateError(f"pivot {pivot_id} has an invalid state transition")
        for timestamp in ("locally_confirmed_at", "paired_confirmed_at", "terminal_at"):
            if before[timestamp] is not None and before[timestamp] != after[timestamp]:
                raise MarketStructureStateError(f"pivot {pivot_id} changed its first {timestamp}")
        if before["state"] == "PAIRED_CONFIRMED" and before["price_estimate"] != after["price_estimate"]:
            raise MarketStructureStateError(f"paired pivot {pivot_id} changed its price")
    for pivot_id, item in new.items():
        if pivot_id not in old and _parse_time(item["first_seen_at"], "first_seen_at") <= prior_as_of:
            raise MarketStructureStateError("new pivots cannot be backdated before the previous snapshot")


def _validate_defense_transition(before: Any, after: Any, field: str) -> None:
    if before is None:
        return
    if after is None:
        if before["status"] == "ACTIVE":
            raise MarketStructureStateError(f"{field} active defense cannot disappear")
        return
    if before["pivot_id"] != after["pivot_id"]:
        if before["status"] == "ACTIVE":
            raise MarketStructureStateError(f"{field} must close the old defense before replacing it")
        return
    for immutable in ("direction", "pivot_id", "qualified_at"):
        if before[immutable] != after[immutable]:
            raise MarketStructureStateError(f"{field} changed immutable field {immutable}")
    allowed = {"ACTIVE": {"ACTIVE", "BROKEN", "REPLACED"}, "BROKEN": {"BROKEN"}, "REPLACED": {"REPLACED"}}
    if after["status"] not in allowed[before["status"]]:
        raise MarketStructureStateError(f"{field} has an invalid status transition")


def _validate_boundary_transition(before: Any, after: Any, field: str) -> None:
    if before is None:
        return
    if after is None:
        if before["status"] == "ACTIVE":
            raise MarketStructureStateError(f"{field} active boundary cannot disappear")
        return
    if before["kind"] != after["kind"] or before["direction"] != after["direction"]:
        if before["status"] == "ACTIVE":
            raise MarketStructureStateError(f"{field} must close the old boundary before replacing it")
        return
    if before["established_at"] != after["established_at"]:
        # A directional slot may be reused only after the prior boundary has
        # reached a terminal state.  ``established_at`` identifies the
        # boundary instance, so a new timestamp means replacement rather than
        # mutation.  An ACTIVE boundary must still be closed in one transition
        # before another boundary may occupy the slot.
        if before["status"] == "ACTIVE":
            raise MarketStructureStateError(f"{field} must close the old boundary before replacing it")
        return
    allowed = {"ACTIVE": {"ACTIVE", "BROKEN", "REPLACED"}, "BROKEN": {"BROKEN"}, "REPLACED": {"REPLACED"}}
    if after["status"] not in allowed[before["status"]]:
        raise MarketStructureStateError(f"{field} has an invalid status transition")


def _validate_scenario_transition(before: Mapping[str, Any], after: Mapping[str, Any], prior_as_of: datetime) -> None:
    old_setup = before["setup"]
    new_setup = after["setup"]
    if old_setup["stage"] == "NONE":
        if new_setup["stage"] != "NONE" and _parse_time(new_setup["first_seen_at"], "setup.first_seen_at") <= prior_as_of:
            raise MarketStructureStateError("new setup cannot backdate first_seen_at")
        return
    if new_setup["stage"] == "NONE":
        if old_setup["stage"] not in TERMINAL_SETUP_STAGES:
            raise MarketStructureStateError("active setup cannot disappear without a terminal stage")
        return
    if old_setup["setup_id"] != new_setup["setup_id"]:
        if old_setup["stage"] not in TERMINAL_SETUP_STAGES:
            raise MarketStructureStateError("active setup must terminate before a new setup replaces it")
        if _parse_time(new_setup["first_seen_at"], "setup.first_seen_at") <= prior_as_of:
            raise MarketStructureStateError("replacement setup cannot be backdated")
        return
    for immutable in ("setup_id", "pattern", "direction", "first_seen_at"):
        if old_setup[immutable] != new_setup[immutable]:
            raise MarketStructureStateError(f"setup changed immutable field {immutable}")
    allowed = {
        "FORMING": {"FORMING", "ARMED", "AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED", "INVALIDATED", "NO_CHASE"},
        "ARMED": {"ARMED", "AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED", "INVALIDATED", "NO_CHASE"},
        "AGGRESSIVE_CONFIRMED": {"AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED", "INVALIDATED", "NO_CHASE"},
        "CONSERVATIVE_CONFIRMED": {"CONSERVATIVE_CONFIRMED", "INVALIDATED", "NO_CHASE"},
        "INVALIDATED": {"INVALIDATED"},
        # A setup that was initially too extended to chase may later invalidate;
        # retaining that causal transition is required for candidate-expiry
        # notifications and must not force the whole analysis to fail.
        "NO_CHASE": {"NO_CHASE", "INVALIDATED"},
    }
    if new_setup["stage"] not in allowed[old_setup["stage"]]:
        raise MarketStructureStateError("setup has an invalid stage transition")
    if old_setup["stage"] == new_setup["stage"]:
        if old_setup["stage_changed_at"] != new_setup["stage_changed_at"]:
            raise MarketStructureStateError("unchanged setup stage cannot rewrite stage_changed_at")
    else:
        changed = _parse_time(new_setup["stage_changed_at"], "setup.stage_changed_at")
        if changed <= prior_as_of:
            raise MarketStructureStateError("setup stage transition cannot be backdated")


def _enum(value: Any, allowed: set[str], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise MarketStructureStateError(f"{field} is invalid")
    return value


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise MarketStructureStateError(f"{field} is invalid")
    return value.strip()


def _optional_positive_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise MarketStructureStateError(f"{field} must be a positive number or null")
    return float(value)


def _optional_time(value: Any, field: str) -> str | None:
    return None if value is None else _iso_time(value, field)


def _iso_time(value: Any, field: str) -> str:
    return _parse_time(value, field).isoformat()


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise MarketStructureStateError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketStructureStateError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MarketStructureStateError(f"{field} must include a timezone")
    return parsed
