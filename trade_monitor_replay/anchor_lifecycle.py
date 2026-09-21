from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from trade_monitor.pivot_replay import Candle, detect_local_pivots


# Kept only for reproducing pre-course-chain experiments. These are NOT course
# rules and are never used by the current course-chain execution path.
LEGACY_MAJOR_REVERSAL_POINTS = 120.0
LEGACY_CHILD_REVERSAL_POINTS = 30.0


class AnchorLifecycleError(ValueError):
    pass


def build_anchor_lifecycle(
    bars: Sequence[Mapping[str, Any]],
    *,
    expected_as_of: str,
    session_key: str,
    major_reversal_points: float | None = None,
    child_reversal_points: float | None = None,
    course_chain_enabled: bool = False,
    session_start: str | None = None,
) -> dict[str, Any]:
    """Build course-style anchor roles from the currently revealed bars only.

    An anchor is a durable directional background.  A newly observed opposite
    swing is a correction/reverse candidate until it completes an opposite
    impulse-pullback-continuation sequence.  It must not silently replace the
    background merely because it is the newest paired pivot leg.

    The calculation is intentionally stateless: rebuilding the same historical
    prefix produces the same IDs and first-observable timestamps.  The runner
    persists the returned snapshot for audit and rewind.
    """

    expected = _aware(expected_as_of, "expected_as_of")
    normalized = _normalize_bars(bars, expected=expected)
    if session_start:
        start = _aware(session_start, "session_start")
        normalized = [bar for bar in normalized if _aware(bar["time"], "bar.time") >= start]
    if course_chain_enabled and (major_reversal_points is not None or child_reversal_points is not None):
        raise AnchorLifecycleError("課程主鏈不接受固定點數轉折門檻。")
    major_reversal_points = LEGACY_MAJOR_REVERSAL_POINTS if major_reversal_points is None else major_reversal_points
    child_reversal_points = LEGACY_CHILD_REVERSAL_POINTS if child_reversal_points is None else child_reversal_points
    empty = {
        "version": 4 if course_chain_enabled else 1,
        "as_of": expected.isoformat(),
        "session_key": session_key,
        "thresholds": {} if course_chain_enabled else {
            "major_reversal_points": float(major_reversal_points),
            "child_reversal_points": float(child_reversal_points),
        },
        "structure_method": "CAUSAL_N2_AND_SAME_KIND_N1" if course_chain_enabled else "LEGACY_FIXED_POINTS",
        "background_anchor": None,
        "child_anchor": None,
        "working_leg": None,
        "reverse_candidate": None,
        "anchor_history": [],
        "dow_context": None,
        "quadrant_context": None,
        "taiji_context": None,
        "events": [],
        "session_scope": {
            "mode": "SESSION_SCOPED" if session_start else "CALLER_SCOPED",
            "start": session_start,
            "prior_session_role": "REFERENCE_ONLY" if session_start else None,
        },
    }
    if len(normalized) < 2:
        return empty

    major_pivots = (
        _course_pivots(normalized, level="LARGE") if course_chain_enabled
        else _zigzag(normalized, threshold=float(major_reversal_points))
    )
    major_history: list[dict[str, Any]] = []
    background = _derive_directional_anchor(
        major_pivots,
        bars=normalized,
        session_key=session_key,
        level="LARGE",
        history_out=major_history if course_chain_enabled else None,
        require_closed_confirmation=course_chain_enabled,
    )
    opening_anchor = _opening_anchor(
        normalized,
        session_start=session_start,
        session_key=session_key,
        threshold=0.0 if course_chain_enabled else float(child_reversal_points),
    )
    if course_chain_enabled and opening_anchor is not None and _origin_breach(opening_anchor, normalized):
        # A fallback cannot restore a session-opening anchor whose origin has
        # already failed. A later directional structure must qualify afresh.
        opening_anchor = None
    if course_chain_enabled:
        major_history = [_historical_anchor(item, normalized) for item in major_history]
        empty["anchor_history"] = major_history
    if course_chain_enabled and background is None and opening_anchor is not None:
        background = _opening_parent_anchor(opening_anchor, major_pivots, normalized, session_key=session_key)
    structural_child_history: list[dict[str, Any]] = []
    if course_chain_enabled and background is None:
        structural_child = _derive_directional_anchor(
            _course_pivots(normalized, level="SMALL"), bars=normalized,
            session_key=session_key, level="SMALL",
            history_out=structural_child_history,
            require_closed_confirmation=True,
        )
        if structural_child is not None:
            if opening_anchor is None:
                opening_anchor = structural_child
            elif opening_anchor["direction"] != structural_child["direction"]:
                guard = opening_anchor.get("defense")
                boundary = float(guard["price"]) if guard else float(opening_anchor["origin_price"])
                after = [b for b in normalized if b["time"] >= structural_child["first_seen_at"]]
                crossed = next((b for b in after if (
                    b["close"] > boundary if structural_child["direction"] == "BULL" else b["close"] < boundary
                )), None)
                if crossed:
                    structural_child["first_seen_at"] = crossed["time"]
                    structural_child["replaced_anchor_ref"] = opening_anchor["id"]
                    opening_anchor = structural_child
    if course_chain_enabled and background is None and opening_anchor is not None:
        # The old parent can be invalidated one or more bars after its defense
        # was broken.  Rebuilding the prefix must not then forget an opposite
        # child that had already completed a Type-2 takeover.  Recover that
        # promoted dynasty from the terminated parent's frozen history.
        promotion_candidates = [*structural_child_history, opening_anchor]
        # Reconstruct the first causal takeover and then replay later child
        # dynasties through it.  Choosing the newest independently promotable
        # child would move a durable background origin forward every time a
        # same-direction copy completed.
        terminated_parents = sorted(
            (
                item
                for item in major_history
                if item.get("status") in {"INVALIDATED", "REPLACED"}
                and isinstance(item.get("defense"), Mapping)
                and item["defense"].get("broken_at")
            ),
            key=lambda item: (
                str(item.get("ended_at") or ""),
                str(item.get("first_seen_at") or ""),
            ),
            reverse=True,
        )
        for parent in terminated_parents:
            recovered, _ = _promote_background_through_child_sequence(
                parent,
                promotion_candidates,
                bars=normalized,
                session_key=session_key,
            )
            if recovered.get("id") != parent.get("id"):
                background = recovered
                break
    if background is None:
        # Before OR5 closes there is only opening evidence, not an anchor.
        # Once OR5 qualifies an opening anchor it controls the small grade;
        # the prior session remains read-only reference and cannot fill the
        # missing large-grade slot.
        if opening_anchor is None:
            if session_start is None:
                empty["working_leg"] = _unclassified_working_leg(
                    major_pivots,
                    session_key=session_key,
                    level="LARGE",
                    as_of=expected,
                )
            empty["events"] = _lifecycle_events(background=None, child=None, candidate=None,
                history=major_history, session_key=session_key)
            return empty
        child_anchor = _apply_defense_lifecycle(opening_anchor, normalized)
        if course_chain_enabled and child_anchor.get("qualification") == "OR5_CLOSED":
            _extend_opening_extreme(child_anchor, normalized)
        elif course_chain_enabled:
            # When no formal large anchor exists, a qualified structural child
            # is returned through this early branch.  It needs the same factual
            # monotonic-extreme protection as the normal background/child path.
            _extend_active_anchor_extreme(child_anchor, normalized)
        reverse_candidate = _reverse_candidate(
            child_anchor,
            normalized,
            threshold=float(child_reversal_points),
            session_key=session_key,
            as_of=expected,
            require_n2_structure=course_chain_enabled,
        )
        if isinstance(reverse_candidate, dict):
            reverse_candidate["level"] = "SMALL"
            reverse_candidate["first_seen_at"] = max(
                _aware(reverse_candidate["first_seen_at"], "candidate.first_seen_at"),
                _aware(child_anchor["first_seen_at"], "child.first_seen_at"),
            ).isoformat()
        working_leg = _working_leg(
            child_anchor,
            reverse_candidate,
            normalized,
            session_key=session_key,
            as_of=expected,
            threshold=float(child_reversal_points),
            strict_course_pivots=course_chain_enabled,
        )
        dow_context = quadrant_context = taiji_context = None
        if course_chain_enabled:
            child_pivots = _course_pivots(normalized, level="SMALL")
            dow_context = _course_dow_context(
                background=None,
                child=child_anchor,
                history=major_history,
                small_pivots=child_pivots,
                bars=normalized,
                session_key=session_key,
            )
            quadrant_context = _course_quadrant_context(
                background=child_anchor,
                child=None,
                working=working_leg,
                bars=normalized,
            )
            taiji_context = _course_taiji_context(
                background=child_anchor,
                child=None,
                working=working_leg,
                candidate=reverse_candidate,
                bars=normalized,
            )
        events = _lifecycle_events(
            background=None,
            child=child_anchor,
            candidate=reverse_candidate,
            history=major_history,
            session_key=session_key,
        )
        return {
            **empty,
            "child_anchor": child_anchor,
            "working_leg": working_leg,
            "reverse_candidate": reverse_candidate,
            "dow_context": dow_context,
            "quadrant_context": quadrant_context,
            "taiji_context": taiji_context,
            "events": events,
        }

    if course_chain_enabled:
        _extend_active_anchor_extreme(background, normalized)
    background = _apply_defense_lifecycle(background, normalized)

    # A child anchor is independently qualified on the small structural scale.
    # It is not the current unfinished leg: once qualified its origin and ID
    # persist until an opposite small structure actually takes control.
    background_origin = _aware(background["origin_time"], "background.origin_time")
    child_bars = [item for item in normalized if _aware(item["time"], "bar.time") >= background_origin]
    child_pivots = (
        _course_pivots(child_bars, level="SMALL") if course_chain_enabled
        else _zigzag(child_bars, threshold=float(child_reversal_points))
    )
    child_history: list[dict[str, Any]] = []
    child_anchor = _derive_directional_anchor(
        child_pivots,
        bars=child_bars,
        session_key=session_key,
        level="SMALL",
        history_out=child_history if course_chain_enabled else None,
        require_closed_confirmation=course_chain_enabled,
    )
    if child_anchor is None and background.get("source_child_anchor_id") and opening_anchor is not None:
        child_anchor = deepcopy(opening_anchor)
        _extend_opening_extreme(child_anchor, normalized)
    if child_anchor is not None:
        if course_chain_enabled:
            _extend_active_anchor_extreme(child_anchor, child_bars)
        child_anchor = _apply_defense_lifecycle(child_anchor, child_bars)

    anchor_history: list[dict[str, Any]] = []
    if course_chain_enabled:
        for historical in major_history:
            anchor_history.append(historical)
        background, replaced_backgrounds = _promote_background_through_child_sequence(
            background,
            [*child_history, child_anchor] if child_anchor is not None else child_history,
            bars=normalized,
            session_key=session_key,
        )
        _extend_active_anchor_extreme(background, normalized)
        anchor_history.extend(replaced_backgrounds)

    reverse_candidate = _reverse_candidate(
        background,
        normalized,
        threshold=float(major_reversal_points),
        session_key=session_key,
        as_of=expected,
        require_n2_structure=course_chain_enabled,
    )
    working_leg = _working_leg(
        background,
        reverse_candidate,
        normalized,
        session_key=session_key,
        as_of=expected,
        threshold=float(child_reversal_points),
        strict_course_pivots=course_chain_enabled,
    )

    dow_context = None
    quadrant_context = None
    taiji_context = None
    if course_chain_enabled:
        dow_context = _course_dow_context(
            background=background,
            child=child_anchor,
            history=anchor_history,
            small_pivots=child_pivots,
            bars=normalized,
            session_key=session_key,
        )
        quadrant_context = _course_quadrant_context(
            background=background,
            child=child_anchor,
            working=working_leg,
            bars=normalized,
        )
        taiji_context = _course_taiji_context(
            background=background,
            child=child_anchor,
            working=working_leg,
            candidate=reverse_candidate,
            bars=normalized,
        )

    events = _lifecycle_events(
        background=background,
        child=child_anchor,
        candidate=reverse_candidate,
        history=anchor_history,
        session_key=session_key,
    )
    return {
        **empty,
        "background_anchor": background,
        "child_anchor": child_anchor,
        "working_leg": working_leg,
        "reverse_candidate": reverse_candidate,
        "anchor_history": anchor_history,
        "dow_context": dow_context,
        "quadrant_context": quadrant_context,
        "taiji_context": taiji_context,
        "events": events,
    }


def anchor_control(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    def reference(key: str) -> str | None:
        value = snapshot.get(key)
        return str(value.get("id")) if isinstance(value, Mapping) and value.get("id") else None

    background = snapshot.get("background_anchor")
    child = snapshot.get("child_anchor")
    dow_context = snapshot.get("dow_context")

    def active_defense(level: str, preferred_direction: str | None) -> Mapping[str, Any] | None:
        if not isinstance(dow_context, Mapping):
            return None
        state = str(dow_context.get(f"{level}_state") or "UNDEFINED")
        direction = preferred_direction
        if state == "BULL":
            direction = "BULL"
        elif state == "BEAR":
            direction = "BEAR"
        elif state == "CONFLICT" or state == "UNDEFINED":
            return None
        value = dow_context.get(f"{level}_{str(direction or '').lower()}_defense")
        if isinstance(value, Mapping) and value.get("state") == "ACTIVE":
            return value
        return None

    background_direction = str(background.get("direction") or "") if isinstance(background, Mapping) else None
    child_direction = str(child.get("direction") or "") if isinstance(child, Mapping) else None
    background_defense = active_defense("large", background_direction)
    child_defense = active_defense("small", child_direction)
    return {
        "active_background_anchor_ref": reference("background_anchor"),
        "active_child_anchor_ref": reference("child_anchor"),
        "working_leg_ref": reference("working_leg"),
        "reverse_anchor_candidate_ref": reference("reverse_candidate"),
        "large_defense_ref": (
            str(background_defense.get("id"))
            if isinstance(background_defense, Mapping) and background_defense.get("id")
            else None
        ),
        "small_defense_ref": (
            str(child_defense.get("id"))
            if isinstance(child_defense, Mapping) and child_defense.get("id")
            else None
        ),
    }


def anchor_records(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for key in ("background_anchor", "child_anchor", "working_leg", "reverse_candidate"):
        value = snapshot.get(key)
        if isinstance(value, Mapping) and value.get("id"):
            records.append(dict(value))
    for value in snapshot.get("anchor_history") or []:
        if isinstance(value, Mapping) and value.get("id"):
            records.append(dict(value))
    return records


def _promote_reverse_child(
    background: Mapping[str, Any],
    child: Mapping[str, Any] | None,
    *,
    bars: list[dict[str, Any]],
    session_key: str,
) -> dict[str, Any] | None:
    """Promote a completed opposite child trend after it breaks the big defense.

    The smaller scale already proves impulse-pullback-continuation.  Once its
    continuation closes through the active large defense, requiring another
    120-point pullback would delay the course Type-2 reversal by a full swing.
    A later reclaim of the old line is a correction inside the new background;
    it does not resurrect the replaced dynasty.
    """

    if not isinstance(child, Mapping) or child.get("direction") == background.get("direction"):
        return None
    defense = background.get("defense")
    if not isinstance(defense, Mapping) or not defense.get("broken_at"):
        return None
    direction = str(child.get("direction") or "")
    latest_price = float(child.get("latest_extreme_price") or 0)
    defense_price = float(defense.get("price") or 0)
    crossed = latest_price < defense_price if direction == "BEAR" else latest_price > defense_price
    if not crossed:
        return None
    break_time = _aware(defense["broken_at"], "background.defense.broken_at")
    child_seen = _aware(child["first_seen_at"], "child.first_seen_at")
    first_seen = max(break_time, child_seen).isoformat()
    origin = {
        "time": child["origin_time"],
        "price": float(child["origin_price"]),
    }
    first_extreme = {
        "time": child["first_extreme_time"],
        "price": float(child["first_extreme_price"]),
    }
    # The origin of the first confirmed large reverse attack remains the
    # large defense until a later same-grade correction causes a new extreme.
    promoted = _new_anchor(
        direction=direction,
        origin=origin,
        first_extreme=first_extreme,
        defense=origin,
        first_seen_at=first_seen,
        session_key=session_key,
        level="LARGE",
    )
    promoted["latest_extreme_time"] = child["latest_extreme_time"]
    promoted["latest_extreme_price"] = float(child["latest_extreme_price"])
    promoted["takeover_type"] = "TYPE2"
    promoted["replaced_anchor_ref"] = background["id"]
    promoted["takeover_reason"] = "OPPOSITE_CHILD_CONTINUATION_BROKE_LARGE_DEFENSE"
    _refresh_anchor_metrics(promoted)
    return _apply_defense_lifecycle(promoted, bars)


def _promote_background_through_child_sequence(
    background: Mapping[str, Any],
    children: Sequence[Mapping[str, Any] | None],
    *,
    bars: list[dict[str, Any]],
    session_key: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Replay child dynasties in causal order so a later child cannot rewrite history.

    Rebuilding a longer prefix may make the latest small anchor change from bull
    to bear and back again.  Looking only at that final child can incorrectly
    replace an already-promoted background with the newest same-direction leg.
    The course role is durable: promote the first opposite child that actually
    breaks the current large defense, then let later same-direction children
    extend that anchor without changing its origin or ID.
    """

    current = deepcopy(dict(background))
    replaced: list[dict[str, Any]] = []
    ordered = sorted(
        (
            dict(item)
            for item in children
            if isinstance(item, Mapping) and item.get("id")
        ),
        key=lambda item: (
            str(item.get("first_seen_at") or ""),
            str(item.get("origin_time") or ""),
            str(item.get("id") or ""),
        ),
    )
    seen: set[str] = set()
    for child in ordered:
        child_id = str(child.get("id"))
        if child_id in seen:
            continue
        seen.add(child_id)
        if child.get("direction") == current.get("direction"):
            latest = child.get("latest_extreme_price")
            if (
                isinstance(latest, (int, float))
                and not isinstance(latest, bool)
                and _more_favorable(
                    str(current.get("direction")),
                    float(latest),
                    float(current.get("latest_extreme_price")),
                )
            ):
                origin_price = float(child.get("origin_price"))
                if _origin_survives(current, origin_price):
                    _extend_anchor(
                        current,
                        extreme={
                            "time": child.get("latest_extreme_time"),
                            "price": float(latest),
                        },
                        defense={
                            "time": child.get("origin_time"),
                            "price": origin_price,
                        },
                    )
                    if isinstance(current.get("defense"), dict):
                        current["defense"]["first_seen_at"] = str(
                            child.get("first_seen_at") or child.get("origin_time")
                        )
                    current = _apply_defense_lifecycle(current, bars)
            continue
        defense = current.get("defense")
        break_at = defense.get("broken_at") if isinstance(defense, Mapping) else None
        if (
            break_at
            and child.get("ended_at")
            and _aware(child["ended_at"], "child.ended_at")
            < _aware(break_at, "defense.broken_at")
        ):
            continue
        promoted = _promote_reverse_child(
            current,
            child,
            bars=bars,
            session_key=session_key,
        )
        if promoted is None:
            continue
        previous = deepcopy(current)
        previous["status"] = "REPLACED"
        previous["ended_at"] = promoted["first_seen_at"]
        previous["replaced_by"] = promoted["id"]
        replaced.append(previous)
        current = promoted
    return current, replaced


def _promote_from_terminated_history(
    history: Sequence[Mapping[str, Any]],
    child: Mapping[str, Any],
    *,
    bars: list[dict[str, Any]],
    session_key: str,
) -> dict[str, Any] | None:
    """Preserve a Type-2 takeover after the displaced parent's origin breaks."""

    candidates = [
        item
        for item in history
        if item.get("direction") != child.get("direction")
        and item.get("status") in {"INVALIDATED", "REPLACED"}
        and isinstance(item.get("defense"), Mapping)
        and item["defense"].get("broken_at")
    ]
    candidates.sort(
        key=lambda item: (
            str(item.get("ended_at") or ""),
            str(item.get("first_seen_at") or ""),
            str(item.get("id") or ""),
        )
    )
    for parent in reversed(candidates):
        defense = parent.get("defense")
        break_at = defense.get("broken_at") if isinstance(defense, Mapping) else None
        child_ended_at = child.get("ended_at")
        if break_at and child_ended_at and _aware(child_ended_at, "child.ended_at") < _aware(break_at, "defense.broken_at"):
            # A child dynasty that ended before the parent defense broke did
            # not cause this takeover, even if its old extreme lies beyond the
            # line when viewed later.
            continue
        promoted = _promote_reverse_child(
            parent,
            child,
            bars=bars,
            session_key=session_key,
        )
        if promoted is not None:
            return promoted
    return None


def _course_dow_context(
    *,
    background: Mapping[str, Any] | None,
    child: Mapping[str, Any] | None,
    history: list[dict[str, Any]],
    small_pivots: list[dict[str, Any]],
    bars: list[dict[str, Any]],
    session_key: str,
) -> dict[str, Any]:
    """Keep bull and bear defenses at both grades instead of one latest line."""

    slots: dict[str, dict[str, Any] | None] = {
        "large_bull": None,
        "large_bear": None,
        "small_bull": None,
        "small_bear": None,
    }

    def publish(slot: str, value: Mapping[str, Any]) -> None:
        """Keep the newest causally qualified defense at the same grade/side.

        A child anchor retains its original defense for lifecycle history, but
        a later LL/LH or HH/HL sequence can establish a newer same-grade Dow
        defense.  The historical anchor record must not overwrite that newer
        line in the live directional slot.
        """

        normalized = dict(value)
        existing = slots.get(slot)
        if not isinstance(existing, Mapping):
            slots[slot] = normalized
            return

        def rank(item: Mapping[str, Any]) -> tuple[datetime, datetime, str]:
            observed_at = item.get("first_seen_at") or item.get("time")
            first_seen = _aware(
                observed_at,
                "defense.first_seen_at",
            )
            source_time = _aware(item.get("time"), "defense.time")
            return first_seen, source_time, str(item.get("id") or "")

        if rank(normalized) >= rank(existing):
            slots[slot] = normalized

    for old in history:
        defense = old.get("defense")
        direction = str(old.get("direction") or "").lower()
        if direction in {"bull", "bear"} and isinstance(defense, Mapping):
            publish(
                f"large_{direction}",
                {**dict(defense), "level": "LARGE", "role": "HISTORICAL_BROKEN"},
            )
    if isinstance(background, Mapping):
        defense = background.get("defense")
        direction = str(background.get("direction") or "").lower()
        if direction in {"bull", "bear"} and isinstance(defense, Mapping):
            publish(
                f"large_{direction}",
                {**dict(defense), "level": "LARGE", "role": "ACTIVE_BACKGROUND"},
            )

    opening_child = (
        isinstance(child, Mapping)
        and child.get("qualification") == "OR5_CLOSED"
    )
    # The generic zigzag cannot know the intrabar order of the opening bar and
    # may discard the true OR5 extreme. While the OR5 anchor controls, accept
    # only a defense qualified by the explicit continuation test above.
    detected_small = {} if opening_child else _directional_defenses(
        small_pivots,
        bars=bars,
        session_key=session_key,
        level="SMALL",
    )
    for direction_name, value in detected_small.items():
        publish(f"small_{direction_name.lower()}", value)
    if isinstance(child, Mapping):
        defense = child.get("defense")
        direction = str(child.get("direction") or "").lower()
        if direction in {"bull", "bear"} and isinstance(defense, Mapping):
            publish(
                f"small_{direction}",
                {**dict(defense), "level": "SMALL", "role": "ACTIVE_CHILD"},
            )

    background_direction = str(background.get("direction") or "UNDEFINED") if isinstance(background, Mapping) else "UNDEFINED"
    child_direction = str(child.get("direction") or "UNDEFINED") if isinstance(child, Mapping) else "UNDEFINED"
    large_bull_active = isinstance(slots["large_bull"], Mapping) and slots["large_bull"].get("state") == "ACTIVE"
    large_bear_active = isinstance(slots["large_bear"], Mapping) and slots["large_bear"].get("state") == "ACTIVE"
    small_bull_active = isinstance(slots["small_bull"], Mapping) and slots["small_bull"].get("state") == "ACTIVE"
    small_bear_active = isinstance(slots["small_bear"], Mapping) and slots["small_bear"].get("state") == "ACTIVE"
    large_state = _resolve_dow_state(
        base_direction=background_direction,
        bull_active=large_bull_active,
        bear_active=large_bear_active,
    )
    small_state = _resolve_dow_state(
        base_direction=child_direction,
        bull_active=small_bull_active,
        bear_active=small_bear_active,
    )
    return {
        "large_state": large_state,
        "small_state": small_state,
        "large_bull_defense": slots["large_bull"],
        "large_bear_defense": slots["large_bear"],
        "small_bull_defense": slots["small_bull"],
        "small_bear_defense": slots["small_bear"],
    }


def _resolve_dow_state(*, base_direction: str, bull_active: bool, bear_active: bool) -> str:
    """Resolve formal Dow direction from live defenses, not a stale anchor label."""

    if bull_active and bear_active:
        if base_direction == "BULL":
            return "BULL_WITH_BEAR_REVERSAL"
        if base_direction == "BEAR":
            return "BEAR_WITH_BULL_REVERSAL"
        return "CONFLICT"
    if bull_active:
        return "BULL"
    if bear_active:
        return "BEAR"
    return "UNDEFINED"


def _directional_defenses(
    pivots: list[dict[str, Any]],
    *,
    bars: list[dict[str, Any]],
    session_key: str,
    level: str,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index in range(3, len(pivots)):
        a, b, c, d = pivots[index - 3 : index + 1]
        if not _strictly_increasing_pivot_times((a, b, c, d)):
            continue
        kinds = [item["kind"] for item in (a, b, c, d)]
        direction: str | None = None
        if kinds == ["LOW", "HIGH", "LOW", "HIGH"] and float(d["price"]) > float(b["price"]):
            direction = "BULL"
        elif kinds == ["HIGH", "LOW", "HIGH", "LOW"] and float(d["price"]) < float(b["price"]):
            direction = "BEAR"
        if direction is None:
            continue
        known_at = _closed_anchor_confirmation(
            (a, b, c), direction=direction, bars=bars,
            until=pivots[index + 1]["time"] if index + 1 < len(pivots) else None,
        )
        if known_at is None:
            continue
        record = {
            "id": _stable_id("DOW_DEFENSE", session_key, level, direction, c["time"]),
            "level": level,
            "direction": direction,
            "time": c["time"],
            "price": float(c["price"]),
            "first_seen_at": known_at,
            "state": "ACTIVE",
            "broken_at": None,
            "reclaimed_at": None,
            "role": "DIRECTIONAL_SLOT",
        }
        result[direction] = _apply_standalone_defense_lifecycle(record, bars)
    return result


def _apply_standalone_defense_lifecycle(
    defense: dict[str, Any], bars: list[dict[str, Any]]
) -> dict[str, Any]:
    direction = str(defense["direction"])
    active_at = _aware(defense["first_seen_at"], "defense.first_seen_at")
    price = float(defense["price"])
    later = [item for item in bars if _aware(item["time"], "bar.time") > active_at]
    broken = next(
        (
            item for item in later
            if (direction == "BULL" and float(item["close"]) < price)
            or (direction == "BEAR" and float(item["close"]) > price)
        ),
        None,
    )
    if broken is None:
        return defense
    defense["state"] = "BROKEN"
    defense["broken_at"] = str(broken["time"])
    return defense


def _course_quadrant_context(
    *,
    background: Mapping[str, Any],
    child: Mapping[str, Any] | None,
    working: Mapping[str, Any] | None,
    bars: list[dict[str, Any]],
) -> dict[str, Any]:
    """Classify the two quadrant axes from same-grade structural comparisons.

    The course does not publish a universal points or bar-count threshold.  The
    replay policy therefore uses only ordinal, scale-invariant facts: whether a
    same-direction leg extends its predecessor, and whether both amplitude and
    slope expand or contract.  Conflicting measures remain ``UNSTABLE`` instead
    of being resolved by an arbitrary numeric cutoff.  This is an explicit
    replay execution policy, not a claim that the instructor supplied a hidden
    formula.
    """

    background_amplitude = abs(float(background.get("amplitude_points") or 0))
    working_amplitude = abs(float((working or {}).get("amplitude_points") or 0))
    ratio = working_amplitude / background_amplitude if background_amplitude else 0.0
    countertrend = isinstance(working, Mapping) and working.get("direction") != background.get("direction")
    background_evidence = _anchored_leg_evidence(
        background, bars, structural_child=child
    )
    working_anchor = child if isinstance(child, Mapping) else background
    working_evidence = (
        _anchored_leg_evidence(child, bars)
        if isinstance(child, Mapping)
        else background_evidence
    )
    background_assessment = _deterministic_quadrant_assessment(
        evidence=background_evidence,
        anchor_direction=str(background.get("direction") or ""),
    )
    working_assessment = _deterministic_quadrant_assessment(
        evidence=working_evidence,
        anchor_direction=str(working_anchor.get("direction") or ""),
    )
    return {
        "authority": "PROGRAM_POLICY_V1",
        "reference_anchor_id": background.get("id"),
        "reference_grade": background.get("level"),
        "anchor_direction": background.get("direction"),
        "phase": "CORRECTION" if countertrend else "PUSH",
        "retracement_ratio": round(ratio, 4),
        "background_primary": background_assessment["primary"],
        "background_confidence": background_assessment["confidence"],
        "background_candidates": background_assessment["candidates"],
        "background_trend_dynamics": background_assessment["trend_dynamics"],
        "background_volatility_dynamics": background_assessment["volatility_dynamics"],
        "working_direction": working.get("direction") if isinstance(working, Mapping) else None,
        # The active same-grade anchor owns the small-structure direction.
        # ``working`` is only the newest forming leg and can be the opposite
        # correction.  This also covers the early-session branch where the
        # small anchor is passed as ``background`` and ``child`` is None.
        "working_structure_direction": working_anchor.get("direction"),
        "working_phase": (
            "CORRECTION"
            if isinstance(working, Mapping)
            and working_anchor.get("direction") != working.get("direction")
            else "PUSH"
        ),
        "working_primary": working_assessment["primary"],
        "working_confidence": working_assessment["confidence"],
        "working_candidates": working_assessment["candidates"],
        "working_trend_dynamics": working_assessment["trend_dynamics"],
        "working_volatility_dynamics": working_assessment["volatility_dynamics"],
        "same_grade_comparisons": background_evidence,
        "child_comparisons": working_evidence if isinstance(child, Mapping) else None,
        "background_reason_codes": background_assessment["reason_codes"],
        "working_reason_codes": working_assessment["reason_codes"],
        "rule": "PROGRAM_ORDINAL_SAME_GRADE_POLICY_V1_NO_FIXED_POINTS_OR_BARS",
    }


def _ordinal_relation(current: Any, previous: Any) -> str:
    try:
        left = float(current)
        right = float(previous)
    except (TypeError, ValueError):
        return "UNKNOWN"
    if left > right:
        return "GREATER"
    if left < right:
        return "LESS"
    return "EQUAL"


def _directional_extension(
    current: Mapping[str, Any],
    previous: Mapping[str, Any],
    direction: str,
) -> bool:
    try:
        current_end = float(current["end_price"])
        previous_end = float(previous["end_price"])
    except (KeyError, TypeError, ValueError):
        return False
    return current_end > previous_end if direction == "BULL" else current_end < previous_end


def _quadrant_from_axes(trend: str, volatility: str) -> str | None:
    return {
        ("INCREASING", "EXPANDING"): "Q1",
        ("DECREASING", "EXPANDING"): "Q2",
        ("DECREASING", "CONTRACTING"): "Q3",
        ("INCREASING", "CONTRACTING"): "Q4",
    }.get((trend, volatility))


def _deterministic_quadrant_assessment(
    *,
    evidence: Mapping[str, Any],
    anchor_direction: str,
) -> dict[str, Any]:
    """Return a causal, scale-invariant dynamic quadrant assessment.

    A forming same-direction copy that has not yet extended its parent remains
    uncertain; it is never declared a failed trend early.  A counter-direction
    correction preserves trend while it stays inside its parent impulse.  The
    volatility axis requires amplitude and slope to agree; disagreement is
    retained as ``UNSTABLE`` with both compatible quadrant candidates.
    """

    raw_legs = evidence.get("legs")
    legs = [item for item in raw_legs if isinstance(item, Mapping)] if isinstance(raw_legs, list) else []
    if anchor_direction not in {"BULL", "BEAR"} or not legs:
        return {
            "primary": "UNDEFINED",
            "confidence": "INSUFFICIENT",
            "candidates": [],
            "trend_dynamics": "UNCLEAR",
            "volatility_dynamics": "UNCLEAR",
            "reason_codes": ["NO_COMPARABLE_ANCHORED_LEG"],
        }

    current = legs[-1]
    current_direction = str(current.get("direction") or "")
    prior_same = next(
        (item for item in reversed(legs[:-1]) if item.get("direction") == current_direction),
        None,
    )
    parent_attack = next(
        (item for item in reversed(legs[:-1]) if item.get("direction") == anchor_direction),
        None,
    )
    reason_codes: list[str] = []

    if current_direction == anchor_direction:
        if prior_same is None:
            trend = "INCREASING"
            volatility = "EXPANDING"
            reason_codes.extend(["INITIAL_DIRECTIONAL_IMPULSE", "INITIAL_VOLATILITY_EXPANSION"])
        else:
            extends = _directional_extension(current, prior_same, anchor_direction)
            if extends:
                trend = "INCREASING"
                reason_codes.append("SAME_GRADE_DIRECTIONAL_EXTREME_EXTENDED")
            elif current.get("status") == "FORMING":
                trend = "UNCLEAR"
                reason_codes.append("FORMING_COPY_NOT_YET_AT_PARENT_EXTREME")
            else:
                trend = "DECREASING"
                reason_codes.append("COMPLETED_COPY_DID_NOT_EXTEND_PARENT_EXTREME")
            amplitude = _ordinal_relation(
                current.get("amplitude_points"), prior_same.get("amplitude_points")
            )
            slope = _ordinal_relation(
                current.get("slope_points_per_minute"),
                prior_same.get("slope_points_per_minute"),
            )
            volatility = _volatility_from_ordinal_metrics(amplitude, slope)
            reason_codes.extend([f"AMPLITUDE_{amplitude}", f"SLOPE_{slope}"])
    else:
        if parent_attack is None:
            trend = "UNCLEAR"
            volatility = "UNCLEAR"
            reason_codes.append("CORRECTION_WITHOUT_PARENT_ATTACK")
        else:
            try:
                current_end = float(current["end_price"])
                parent_start = float(parent_attack["start_price"])
                destructive = (
                    current_end <= parent_start
                    if anchor_direction == "BULL"
                    else current_end >= parent_start
                )
            except (KeyError, TypeError, ValueError):
                destructive = False
            trend = "DECREASING" if destructive else "INCREASING"
            reason_codes.append(
                "CORRECTION_BROKE_PARENT_ORIGIN"
                if destructive
                else "CORRECTION_HELD_PARENT_ORIGIN"
            )
            amplitude = _ordinal_relation(
                current.get("amplitude_points"), parent_attack.get("amplitude_points")
            )
            slope = _ordinal_relation(
                current.get("slope_points_per_minute"),
                parent_attack.get("slope_points_per_minute"),
            )
            volatility = _volatility_from_ordinal_metrics(amplitude, slope)
            reason_codes.extend([f"AMPLITUDE_{amplitude}", f"SLOPE_{slope}"])

    primary = _quadrant_from_axes(trend, volatility)
    candidates: list[str] = []
    if primary is not None:
        candidates = [primary]
    elif trend == "INCREASING" and volatility == "UNSTABLE":
        candidates = ["Q1", "Q4"]
    elif trend == "DECREASING" and volatility == "UNSTABLE":
        candidates = ["Q2", "Q3"]
    elif trend == "UNCLEAR" and volatility == "EXPANDING":
        candidates = ["Q1", "Q2"]
    elif trend == "UNCLEAR" and volatility == "CONTRACTING":
        candidates = ["Q4", "Q3"]
    primary_value = primary or ("TRANSITION" if candidates else "UNDEFINED")
    confidence = (
        "HIGH" if primary is not None and len(legs) >= 4
        else "MEDIUM" if primary is not None and len(legs) >= 2
        else "LOW" if primary is not None
        else "TRANSITIONAL" if candidates
        else "INSUFFICIENT"
    )
    return {
        "primary": primary_value,
        "confidence": confidence,
        "candidates": candidates,
        "trend_dynamics": trend,
        "volatility_dynamics": volatility,
        "reason_codes": reason_codes,
    }


def _volatility_from_ordinal_metrics(amplitude: str, slope: str) -> str:
    expanding = {"GREATER", "EQUAL"}
    contracting = {"LESS", "EQUAL"}
    if amplitude in expanding and slope in expanding and not (
        amplitude == slope == "EQUAL"
    ):
        return "EXPANDING"
    if amplitude in contracting and slope in contracting and not (
        amplitude == slope == "EQUAL"
    ):
        return "CONTRACTING"
    if "UNKNOWN" in {amplitude, slope}:
        return "UNCLEAR"
    return "UNSTABLE"


def _same_price(left: Any, right: Any, *, tolerance: float = 0.01) -> bool:
    try:
        return abs(float(left) - float(right)) <= tolerance
    except (TypeError, ValueError):
        return False


def _course_taiji_context(
    *,
    background: Mapping[str, Any],
    child: Mapping[str, Any] | None = None,
    working: Mapping[str, Any] | None,
    candidate: Mapping[str, Any] | None,
    bars: list[dict[str, Any]],
) -> dict[str, Any]:
    background_evidence = _anchored_leg_evidence(
        background, bars, structural_child=child
    )
    operating_anchor = child if isinstance(child, Mapping) else background
    evidence = (
        _anchored_leg_evidence(child, bars)
        if isinstance(child, Mapping)
        else background_evidence
    )
    current_evidence_leg = evidence.get("current_leg")
    relation = "NONE"
    if isinstance(current_evidence_leg, Mapping):
        relation = (
            "COPY"
            if current_evidence_leg.get("direction") == operating_anchor.get("direction")
            else "CORRECTION"
        )
    elif isinstance(working, Mapping):
        relation = (
            "COPY"
            if working.get("direction") == operating_anchor.get("direction")
            else "CORRECTION"
        )
    assessment = _deterministic_taiji_assessment(
        evidence=evidence,
        anchor_direction=str(operating_anchor.get("direction") or ""),
    )
    parent = evidence["current_parent"]
    parent_direction = (
        parent.get("direction") if isinstance(parent, Mapping) else operating_anchor.get("direction")
    )
    parent_start_time = parent.get("start_time") if parent else operating_anchor.get("origin_time")
    parent_start_price = parent.get("start_price") if parent else operating_anchor.get("origin_price")
    parent_start_kind = (
        "SESSION_OPEN"
        if (
            operating_anchor.get("anchor_origin_kind") == "SESSION_OPEN"
            and str(parent_start_time) == str(operating_anchor.get("origin_time"))
            and _same_price(parent_start_price, operating_anchor.get("origin_price"))
        )
        else "LOW" if parent_direction == "BULL" else "HIGH"
    )
    parent_end_kind = "HIGH" if parent_direction == "BULL" else "LOW"
    return {
        "dynasty_anchor_ref": operating_anchor.get("id"),
        "background_dynasty_anchor_ref": background.get("id"),
        "operating_grade": operating_anchor.get("level"),
        "parent_direction": parent_direction,
        "parent_start_time": parent_start_time,
        "parent_start_price": parent_start_price,
        "parent_start_kind": parent_start_kind,
        "parent_end_time": parent.get("end_time") if parent else operating_anchor.get("first_extreme_time"),
        "parent_end_price": parent.get("end_price") if parent else operating_anchor.get("first_extreme_price"),
        "parent_end_kind": parent_end_kind,
        "current_leg_ref": working.get("id") if isinstance(working, Mapping) else None,
        "current_relation": relation,
        "reverse_candidate_ref": candidate.get("id") if isinstance(candidate, Mapping) else None,
        "leg_evidence": evidence,
        "background_leg_evidence": background_evidence,
        "program_state": assessment["state"],
        "program_quality": assessment["quality"],
        "last_copy_status": assessment["last_copy_status"],
        "reason_codes": assessment["reason_codes"],
        "engine_mode": assessment["engine_mode"],
        "assessment_authority": "PROGRAM_POLICY_V1",
    }


def _deterministic_taiji_assessment(
    *,
    evidence: Mapping[str, Any],
    anchor_direction: str,
) -> dict[str, Any]:
    raw_legs = evidence.get("legs")
    legs = [item for item in raw_legs if isinstance(item, Mapping)] if isinstance(raw_legs, list) else []
    if anchor_direction not in {"BULL", "BEAR"} or not legs:
        return {
            "state": "INSUFFICIENT",
            "quality": "UNDEFINED",
            "last_copy_status": "NOT_AVAILABLE",
            "engine_mode": "UNDEFINED",
            "reason_codes": ["NO_ANCHORED_LEG_SEQUENCE"],
        }

    current = legs[-1]
    current_direction = str(current.get("direction") or "")
    relation = "COPY" if current_direction == anchor_direction else "CORRECTION"
    same_before = [item for item in legs[:-1] if item.get("direction") == current_direction]
    parent = same_before[-1] if same_before else None
    reason_codes: list[str] = [f"CURRENT_{relation}"]
    quality = "UNDEFINED"

    if relation == "CORRECTION":
        parent_attack = next(
            (item for item in reversed(legs[:-1]) if item.get("direction") == anchor_direction),
            None,
        )
        destructive = False
        if isinstance(parent_attack, Mapping):
            try:
                destructive = (
                    float(current["end_price"]) <= float(parent_attack["start_price"])
                    if anchor_direction == "BULL"
                    else float(current["end_price"]) >= float(parent_attack["start_price"])
                )
            except (KeyError, TypeError, ValueError):
                destructive = False
        if destructive:
            state = "CORRECTION_DESTRUCTIVE"
            quality = "FAILED"
            reason_codes.append("PARENT_ORIGIN_BROKEN")
        else:
            state = (
                "CORRECTION_FORMING"
                if current.get("status") == "FORMING"
                else "CORRECTION_HELD"
            )
            quality = "ORDERLY" if isinstance(parent_attack, Mapping) else "UNDEFINED"
            reason_codes.append("PARENT_ORIGIN_HELD")
    elif parent is None:
        state = "FIRST_PUSH"
        quality = "BASELINE"
        reason_codes.append("NO_PRIOR_SAME_DIRECTION_PARENT")
    else:
        extends = _directional_extension(current, parent, anchor_direction)
        amplitude = _ordinal_relation(
            current.get("amplitude_points"), parent.get("amplitude_points")
        )
        slope = _ordinal_relation(
            current.get("slope_points_per_minute"), parent.get("slope_points_per_minute")
        )
        cleaner = _ordinal_relation(current.get("wick_fraction"), parent.get("wick_fraction"))
        reason_codes.extend(
            [
                "PARENT_EXTREME_EXTENDED" if extends else "PARENT_EXTREME_NOT_EXTENDED",
                f"AMPLITUDE_{amplitude}",
                f"SLOPE_{slope}",
                f"WICK_FRACTION_{cleaner}",
            ]
        )
        positive_quality = sum(
            (
                amplitude in {"GREATER", "EQUAL"},
                slope in {"GREATER", "EQUAL"},
                cleaner in {"LESS", "EQUAL"},
                extends,
            )
        )
        if current.get("status") == "FORMING":
            state = "COPY_FORMING"
            quality = (
                "PROVISIONALLY_STRONGER" if positive_quality >= 3
                else "PROVISIONALLY_WEAKER" if positive_quality <= 1
                else "PROVISIONALLY_MIXED"
            )
        elif not extends:
            state = "COPY_FAILED"
            quality = "FAILED"
        elif positive_quality >= 3:
            state = "COPY_SUCCESS"
            quality = "STRONG"
        else:
            state = "COPY_WEAKENED"
            quality = "WEAK"

    completed_copies = [
        item
        for item in legs
        if item.get("direction") == anchor_direction and item.get("status") != "FORMING"
    ]
    last_copy_status = "NOT_AVAILABLE"
    if len(completed_copies) >= 2:
        latest = completed_copies[-1]
        prior = completed_copies[-2]
        if not _directional_extension(latest, prior, anchor_direction):
            last_copy_status = "FAILED"
        elif float(latest.get("amplitude_points") or 0) >= float(prior.get("amplitude_points") or 0):
            last_copy_status = "SUCCESS"
        else:
            last_copy_status = "WEAKENED"

    return {
        "state": state,
        "quality": quality,
        "last_copy_status": last_copy_status,
        "engine_mode": "TAIJI_ORDERED" if len(legs) >= 2 else "UNDEFINED",
        "reason_codes": reason_codes,
    }


def _causal_paired_points(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """n=2 local pivots, observed in arrival order; no invented OHLC ordering.

    The last local point remains provisional until its opposite is observed.
    Late confirmations cannot be inserted into an already consumed past leg.
    This is a replay tie/arrival policy, not an instructor-specified formula.
    """
    candles = [Candle(_aware(b["time"], "bar.time"), b["open"], b["high"], b["low"], b["close"]) for b in bars]
    local, _ = detect_local_pivots(candles, 2)
    # Exclude an outside candle as soon as BOTH extremum shapes are visible,
    # not only after both breach confirmations arrive. Otherwise a later
    # opposite breach could retroactively remove an already-used endpoint.
    ambiguous = set()
    for i in range(2, len(candles) - 2):
        neighbors = candles[i - 2:i] + candles[i + 1:i + 3]
        if all(c.high < candles[i].high and c.low > candles[i].low for c in neighbors):
            ambiguous.add(candles[i].at.isoformat())
    result: list[dict[str, Any]] = []
    for p in local:
        if p.bar_time in ambiguous or (result and p.bar_time <= result[-1]["time"]):
            continue
        point = {"kind": p.kind, "time": p.bar_time, "price": p.price,
                 "confirmed": True, "confirmation_time": p.confirmation_time,
                 "first_seen_at": p.confirmation_time, "pair_confirmed_at": None}
        if result and result[-1]["kind"] == p.kind:
            if _more_favorable("BULL" if p.kind == "HIGH" else "BEAR", p.price, result[-1]["price"]):
                result[-1] = point
        else:
            if result:
                result[-1]["pair_confirmed_at"] = p.confirmation_time
            result.append(point)
    return result


def _course_pivots(bars: list[dict[str, Any]], *, level: str) -> list[dict[str, Any]]:
    primary = _causal_paired_points(bars)
    if level == "SMALL":
        points = primary
    else:
        # A higher-grade point compares like kinds, never absolute point size.
        finalized = [p for p in primary if p.get("pair_confirmed_at")]
        candidates = []
        for kind in ("HIGH", "LOW"):
            same = [p for p in finalized if p["kind"] == kind]
            for left, middle, right in zip(same, same[1:], same[2:]):
                qualifies = (middle["price"] > max(left["price"], right["price"]) if kind == "HIGH"
                             else middle["price"] < min(left["price"], right["price"]))
                if qualifies:
                    known = max(p["pair_confirmed_at"] for p in (left, middle, right))
                    candidates.append({**middle, "confirmation_time": known, "first_seen_at": known, "pair_confirmed_at": None})
        points = []
        for p in sorted(candidates, key=lambda p: (p["confirmation_time"], p["time"])):
            if points and p["time"] <= points[-1]["time"]:
                continue
            if points and p["kind"] == points[-1]["kind"]:
                if _more_favorable("BULL" if p["kind"] == "HIGH" else "BEAR", p["price"], points[-1]["price"]):
                    points[-1] = p
            else:
                points.append(p)
    # Current D need not be a future-confirmed pivot to show continuation.
    if points:
        last = points[-1]
        later = [b for b in bars if b["time"] > last["time"]]
        if later:
            kind = "LOW" if last["kind"] == "HIGH" else "HIGH"
            field = kind.lower()
            end = (min if kind == "LOW" else max)(later, key=lambda b: b[field])
            points = points + [{"kind": kind, "time": end["time"], "price": end[field],
                                "confirmed": False, "confirmation_time": None}]
    return points


def _anchored_leg_evidence(
    anchor: Mapping[str, Any],
    bars: list[dict[str, Any]],
    *,
    structural_child: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    points = _course_pivots(bars, level=str(anchor.get("level") or "SMALL"))
    points = [p for p in points if p["time"] > anchor["origin_time"]]
    if (
        isinstance(structural_child, Mapping)
        and structural_child.get("id") != anchor.get("id")
        and structural_child.get("origin_time") > anchor.get("origin_time")
    ):
        child_direction = str(structural_child.get("direction") or "")
        child_kind = "LOW" if child_direction == "BULL" else "HIGH" if child_direction == "BEAR" else None
        if child_kind is not None:
            points.append(
                {
                    "time": structural_child.get("origin_time"),
                    "price": structural_child.get("origin_price"),
                    "kind": child_kind,
                    "confirmed": True,
                    "confirmation_time": structural_child.get("first_seen_at"),
                }
            )
    # Merge adjacent like-kind points before constructing legs.  The more
    # extreme HIGH/LOW retains its own timestamp, while the session anchor
    # origin below remains immutable.  This lets an already qualified child
    # origin restore a real large-grade correction endpoint that sparse
    # like-kind filtering may otherwise skip.
    merged: list[dict[str, Any]] = []
    for point in sorted(points, key=lambda item: item["time"]):
        if merged and point["kind"] == merged[-1]["kind"]:
            favorable = (
                float(point["price"]) > float(merged[-1]["price"])
                if point["kind"] == "HIGH"
                else float(point["price"]) < float(merged[-1]["price"])
            )
            if favorable:
                merged[-1] = point
            continue
        merged.append(point)
    origin_kind = "LOW" if anchor["direction"] == "BULL" else "HIGH"
    if merged and merged[0]["kind"] == origin_kind:
        merged.pop(0)
    points = [{"time": anchor["origin_time"], "price": anchor["origin_price"], "kind": origin_kind,
               "confirmed": True, "confirmation_time": anchor["first_seen_at"]}, *merged]
    legs = []
    for left, right in zip(points, points[1:]):
        if left["kind"] == right["kind"] or left["time"] >= right["time"]:
            continue
        role_direction = (
            "BULL" if left["kind"] == "LOW" and right["kind"] == "HIGH"
            else "BEAR" if left["kind"] == "HIGH" and right["kind"] == "LOW"
            else None
        )
        price_direction = (
            "BULL" if float(right["price"]) > float(left["price"])
            else "BEAR" if float(right["price"]) < float(left["price"])
            else None
        )
        if role_direction is None or price_direction != role_direction:
            # Higher-grade like-kind filtering can leave a later structural
            # LOW above an earlier HIGH (or a later HIGH below an earlier
            # LOW).  Joining those points would manufacture a leg whose price
            # direction contradicts its endpoint roles.  Omit that pair; the
            # following valid LOW->HIGH / HIGH->LOW pair remains available.
            continue
        if (anchor.get("qualification") == "OR5_CLOSED"
            and left["time"] == anchor["origin_time"]
            and not _more_favorable(anchor["direction"], float(right["price"]), float(anchor["first_extreme_price"]))):
            # OR5 starts at the OPEN, not a confirmed turning high/low. The
            # first later local pivot may be inside the initial impulse; it
            # cannot retroactively replace that impulse's actual extreme.
            # Keep the initial anchor as the fallback Taiji reference instead.
            continue
        duration = _minutes(_aware(left["time"], "left"), _aware(right["time"], "right"))
        amplitude = right["price"] - left["price"]
        sample = [b for b in bars if left["time"] < b["time"] <= right["time"]]
        total_range = sum(b["high"] - b["low"] for b in sample)
        wick = sum(b["high"] - b["low"] - abs(b["close"] - b["open"]) for b in sample)
        legs.append({"start_time": left["time"], "start_price": left["price"],
                     "end_time": right["time"], "end_price": right["price"],
                     "direction": role_direction,
                     "amplitude_points": abs(amplitude), "duration_minutes": duration,
                     "slope_points_per_minute": abs(amplitude) / duration if duration else None,
                     "wick_fraction": wick / total_range if total_range else None,
                     "status": "LOCAL_CONFIRMED" if right.get("confirmed") else "FORMING",
                     "observable_at": right.get("confirmation_time") or bars[-1]["time"]})
    comparisons = []
    for index, leg in enumerate(legs):
        prior = next((p for p in reversed(legs[:index]) if p["direction"] == leg["direction"]), None)
        if prior:
            comparisons.append({"current_start": leg["start_time"], "parent_start": prior["start_time"],
                                "kind": "COPY" if leg["direction"] == anchor["direction"] else "CORRECTION",
                                "amplitude_ratio": leg["amplitude_points"] / prior["amplitude_points"] if prior["amplitude_points"] else None,
                                "duration_ratio": leg["duration_minutes"] / prior["duration_minutes"] if prior["duration_minutes"] else None,
                                "current_status": leg["status"]})
    current = legs[-1] if legs else None
    parent = next((p for p in reversed(legs[:-1]) if p["direction"] == anchor["direction"]), None)
    return {"anchor_ref": anchor["id"], "grade": anchor.get("level"), "legs": legs,
            "same_direction_comparisons": comparisons, "current_leg": current, "current_parent": parent,
            "quality_verdict": None, "proportion_filter": "UNQUANTIFIED_NOT_SILENTLY_DELETED"}


def _extend_opening_extreme(anchor: dict[str, Any], bars: list[dict[str, Any]]) -> None:
    """Preserve the initial impulse; separately extend the dynasty's extreme."""
    for bar in bars:
        if bar["time"] <= anchor["qualification_time"]:
            continue
        if not _origin_survives(anchor, bar["close"]):
            # A touch is not a break. Do not resurrect an origin after a break.
            if bar["close"] != anchor["origin_price"]:
                anchor["status"] = "DEGRADED"
                anchor["origin_broken_at"] = bar["time"]
                break
        price = bar["low"] if anchor["direction"] == "BEAR" else bar["high"]
        _extend_anchor(anchor, extreme={"time": bar["time"], "price": price}, defense=None)


def _extend_active_anchor_extreme(
    anchor: dict[str, Any], bars: list[dict[str, Any]]
) -> None:
    """Keep an active anchor's factual favorable extreme monotonic.

    A causal n=2 endpoint can be confirmed after a previously visible raw
    extreme, and that newly confirmed endpoint can be less favorable.  The
    endpoint is still useful for legs/Taiji, but it must not shrink the durable
    anchor that has the same origin and ID.  OHLC order on the origin candle is
    unknown, so only later candles may extend it.
    """

    origin_time = _aware(anchor["origin_time"], "anchor.origin_time")
    for bar in bars:
        bar_time = _aware(bar["time"], "bar.time")
        if bar_time <= origin_time:
            continue
        price = float(bar["high"] if anchor["direction"] == "BULL" else bar["low"])
        _extend_anchor(
            anchor,
            extreme={"time": bar["time"], "price": price},
            defense=None,
        )


def _opening_parent_anchor(
    opening: Mapping[str, Any], pivots: list[dict[str, Any]], bars: list[dict[str, Any]], *, session_key: str,
) -> dict[str, Any] | None:
    """Recognize an opening impulse's structural parent, not a points gate.

    Its directional extreme must itself have become a same-kind n=1 higher
    pivot, and the opening continuation must already have a causal small
    defense. That establishes a parent impulse, NOT a complete large Dow trend
    or a large Dow defense. Further qualitative anchor quality is still reviewed.
    """
    guard = opening.get("defense")
    if not isinstance(guard, Mapping):
        return None
    kind = "LOW" if opening["direction"] == "BEAR" else "HIGH"
    extreme = next((p for p in pivots if p.get("confirmed") and p["kind"] == kind
                    and p["time"] > opening["first_extreme_time"]
                    and _more_favorable(opening["direction"], p["price"], opening["first_extreme_price"])), None)
    if extreme is None:
        return None
    prefix = [b for b in bars if b["time"] <= extreme["confirmation_time"]]
    _, causal_guard = _opening_defense_evidence(prefix, direction=opening["direction"],
        first_extreme={"time": opening["first_extreme_time"], "price": opening["first_extreme_price"]})
    if causal_guard is None:
        return None
    first_seen = max(extreme["confirmation_time"], causal_guard["first_seen_at"])
    result = _new_anchor(direction=opening["direction"],
                         origin={"time": opening["origin_time"], "price": opening["origin_price"]},
                         first_extreme=extreme, defense=None, first_seen_at=first_seen,
                         session_key=session_key, level="LARGE")
    result.update({"qualification": "OPENING_EXTREME_HIGHER_PIVOT",
                   "anchor_origin_kind": "SESSION_OPEN",
                   "source_child_anchor_id": opening["id"],
                   "quality_status": "REQUIRES_ANALYST_REVIEW",
                   "invalidation_boundary": {"time": opening["origin_time"], "price": opening["origin_price"],
                                              "role": "ANCHOR_ORIGIN_NOT_DOW_DEFENSE"}})
    for bar in bars:
        if bar["time"] <= first_seen:
            continue
        if ((opening["direction"] == "BEAR" and bar["close"] > opening["origin_price"])
                or (opening["direction"] == "BULL" and bar["close"] < opening["origin_price"])):
            result["status"] = "DEGRADED"
            result["origin_broken_at"] = bar["time"]
            break
        price = bar["low"] if opening["direction"] == "BEAR" else bar["high"]
        _extend_anchor(result, extreme={"time": bar["time"], "price": price}, defense=None)
    return result


def _zigzag(bars: list[dict[str, Any]], *, threshold: float) -> list[dict[str, Any]]:
    """Return confirmed structural turns plus the current provisional extreme."""

    if not bars:
        return []
    high = _point("HIGH", bars[0], float(bars[0]["high"]), confirmed=False)
    low = _point("LOW", bars[0], float(bars[0]["low"]), confirmed=False)
    mode: str | None = None
    turns: list[dict[str, Any]] = []
    for bar in bars[1:]:
        bar_time = _aware(bar["time"], "bar.time")
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        if mode is None:
            if bar_high > float(high["price"]):
                high = _point("HIGH", bar, bar_high, confirmed=False)
            if bar_low < float(low["price"]):
                low = _point("LOW", bar, bar_low, confirmed=False)
            if float(high["price"]) - float(low["price"]) < threshold:
                continue
            if _aware(low["time"], "low.time") < _aware(high["time"], "high.time"):
                turns.append(_confirm(low, at=bar_time))
                mode = "UP"
                high = _point("HIGH", bar, bar_high, confirmed=False)
            else:
                turns.append(_confirm(high, at=bar_time))
                mode = "DOWN"
                low = _point("LOW", bar, bar_low, confirmed=False)
            continue
        if mode == "UP":
            if bar_high > float(high["price"]):
                high = _point("HIGH", bar, bar_high, confirmed=False)
            if float(high["price"]) - bar_low >= threshold:
                turns.append(_confirm(high, at=bar_time))
                mode = "DOWN"
                low = _point("LOW", bar, bar_low, confirmed=False)
        else:
            if bar_low < float(low["price"]):
                low = _point("LOW", bar, bar_low, confirmed=False)
            if bar_high - float(low["price"]) >= threshold:
                turns.append(_confirm(low, at=bar_time))
                mode = "UP"
                high = _point("HIGH", bar, bar_high, confirmed=False)
    if mode == "UP":
        turns.append(high)
    elif mode == "DOWN":
        turns.append(low)
    else:
        first = bars[0]
        last = bars[-1]
        direction = "HIGH" if float(last["close"]) >= float(first["open"]) else "LOW"
        price = max(float(item["high"]) for item in bars) if direction == "HIGH" else min(float(item["low"]) for item in bars)
        source = next(
            item for item in bars
            if float(item["high"] if direction == "HIGH" else item["low"]) == price
        )
        turns.append(_point(direction, source, price, confirmed=False))
    return _dedupe_turns(turns)


def _opening_anchor(
    bars: list[dict[str, Any]],
    *,
    session_start: str | None,
    session_key: str,
    threshold: float,
) -> dict[str, Any] | None:
    """Qualify a small opening anchor only after five closed session bars.

    The session open is chronologically known, so measuring from that open to
    an OR5 extreme does not invent the unknown high/low order inside one OHLC
    bar.  The role is still withheld until the 08:49/15:04/US-open+4 bar has
    closed; before then the same movement is opening evidence only.
    """

    if not session_start:
        return None
    start = _aware(session_start, "session_start")
    by_minute = {
        _aware(item["time"], "bar.time").replace(second=0, microsecond=0): item
        for item in bars
    }
    window: list[dict[str, Any]] = []
    for offset in range(5):
        item = by_minute.get(start + timedelta(minutes=offset))
        if item is None:
            return None
        window.append(item)

    origin_price = float(window[0]["open"])
    high_price = max(float(item["high"]) for item in window)
    low_price = min(float(item["low"]) for item in window)
    close_price = float(window[-1]["close"])
    upside = high_price - origin_price
    downside = origin_price - low_price
    if downside >= threshold and downside > upside and close_price < origin_price:
        direction = "BEAR"
        extreme_price = low_price
        extreme_bar = next(item for item in window if float(item["low"]) == extreme_price)
    elif upside >= threshold and upside > downside and close_price > origin_price:
        direction = "BULL"
        extreme_price = high_price
        extreme_bar = next(item for item in window if float(item["high"]) == extreme_price)
    else:
        return None

    defense_candidate, qualified_defense = _opening_defense_evidence(
        bars,
        direction=direction,
        first_extreme={"time": extreme_bar["time"], "price": extreme_price},
    )
    anchor = _new_anchor(
        direction=direction,
        origin={"time": window[0]["time"], "price": origin_price},
        first_extreme={"time": extreme_bar["time"], "price": extreme_price},
        defense=qualified_defense,
        first_seen_at=str(window[-1]["time"]),
        session_key=session_key,
        level="SMALL",
    )
    if isinstance(qualified_defense, Mapping) and isinstance(anchor.get("defense"), dict):
        anchor["defense"]["first_seen_at"] = str(qualified_defense["first_seen_at"])
    if isinstance(defense_candidate, Mapping):
        anchor["defense_candidate"] = {
            "id": _stable_id(
                "DOW_DEFENSE_CANDIDATE",
                session_key,
                direction,
                defense_candidate["time"],
            ),
            "direction": direction,
            "time": defense_candidate["time"],
            "price": float(defense_candidate["price"]),
            "state": (
                "QUALIFIED"
                if isinstance(qualified_defense, Mapping)
                and qualified_defense["time"] == defense_candidate["time"]
                else "CANDIDATE"
            ),
            "role": "OPENING_ANCHOR_GUARD_CANDIDATE",
            "qualification_reason": (
                "CAUSED_NEW_LOW" if direction == "BEAR" else "CAUSED_NEW_HIGH"
            ) if isinstance(qualified_defense, Mapping) and qualified_defense["time"] == defense_candidate["time"] else (
                "WAITING_NEW_LOW" if direction == "BEAR" else "WAITING_NEW_HIGH"
            ),
        }
    anchor.update(
        {
            "anchor_origin_kind": "SESSION_OPEN",
            "qualification": "OR5_CLOSED",
            "qualification_time": str(window[-1]["time"]),
            "qualification_duration_minutes": 4,
        }
    )
    return anchor


def _opening_defense_evidence(
    bars: list[dict[str, Any]],
    *,
    direction: str,
    first_extreme: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Separate a local opening guard from a causal Dow defense.

    A counter-swing is only a candidate after it passes the course n=2 pivot
    test, including two complete bars on both sides and the candidate candle's
    opposite endpoint being breached. It earns Dow-defense status
    retrospectively only when a later closed bar creates a new favorable
    extreme beyond the opening anchor's prior extreme. This preserves both
    causal gates: pivot first, result next, defense qualification last.
    """

    extreme_time = _aware(first_extreme["time"], "first_extreme.time")
    scoped = [item for item in bars if _aware(item["time"], "bar.time") >= extreme_time]
    pivot_kind = "HIGH" if direction == "BEAR" else "LOW"
    candidates = [
        {
            "time": item["time"],
            "price": float(item["price"]),
            "pivot_first_seen_at": item["first_seen_at"],
        }
        for item in _confirmed_n2_pivots(scoped)
        if item["kind"] == pivot_kind
        and _aware(item["time"], "pivot.time") > extreme_time
    ]
    if not candidates:
        return None, None

    qualified: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_time = _aware(candidate["time"], "candidate.time")
        before = [
            item
            for item in scoped
            if _aware(item["time"], "bar.time") <= candidate_time
        ]
        pivot_first_seen = _aware(
            candidate["pivot_first_seen_at"],
            "candidate.pivot_first_seen_at",
        )
        after = [
            item
            for item in scoped
            if _aware(item["time"], "bar.time") >= pivot_first_seen
        ]
        if direction == "BEAR":
            prior_extreme = min(float(item["low"]) for item in before)
            continuation = next(
                (item for item in after if float(item["low"]) < prior_extreme),
                None,
            )
        else:
            prior_extreme = max(float(item["high"]) for item in before)
            continuation = next(
                (item for item in after if float(item["high"]) > prior_extreme),
                None,
            )
        if continuation is not None:
            continuation_time = _aware(continuation["time"], "continuation.time")
            qualified.append(
                {
                    **candidate,
                    "first_seen_at": max(pivot_first_seen, continuation_time).isoformat(),
                }
            )
    return candidates[-1], qualified[-1] if qualified else None


def _confirmed_n2_pivots(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the paired course n=2 pivot sequence visible at this cutoff."""

    if len(bars) < 5:
        return []
    return _causal_paired_points(bars)


def _derive_directional_anchor(
    pivots: list[dict[str, Any]],
    *,
    bars: list[dict[str, Any]],
    session_key: str,
    level: str,
    history_out: list[dict[str, Any]] | None = None,
    require_closed_confirmation: bool = False,
) -> dict[str, Any] | None:
    background: dict[str, Any] | None = None
    pending_opposite: dict[str, Any] | None = None
    for index in range(3, len(pivots)):
        a, b, c, d = pivots[index - 3 : index + 1]
        if not _strictly_increasing_pivot_times((a, b, c, d)):
            continue
        kinds = [item["kind"] for item in (a, b, c, d)]
        direction: str | None = None
        if kinds == ["LOW", "HIGH", "LOW", "HIGH"] and float(d["price"]) > float(b["price"]) and float(c["price"]) > float(a["price"]):
            direction = "BULL"
        elif kinds == ["HIGH", "LOW", "HIGH", "LOW"] and float(d["price"]) < float(b["price"]) and float(c["price"]) < float(a["price"]):
            direction = "BEAR"
        if direction is None:
            continue
        # With A/B/C already confirmed, the closed bar that crosses B makes
        # the continuation causal immediately; waiting for a later reversal
        # to confirm D would delay the anchor and turn a useful background
        # event into hindsight.
        first_seen = d["time"]
        if require_closed_confirmation:
            # A wick beyond B describes an extreme, not a confirmed takeover.
            # A/B/C must already be observable before a close can qualify D.
            first_seen = _closed_anchor_confirmation(
                (a, b, c), direction=direction, bars=bars,
                until=pivots[index + 1]["time"] if index + 1 < len(pivots) else None,
            )
            if first_seen is None:
                continue
        if (
            background is not None
            and require_closed_confirmation
            and isinstance(pending_opposite, dict)
            and _origin_breach(pending_opposite, bars, through=str(first_seen)) is None
        ):
            visible = [
                item
                for item in bars
                if _aware(item["time"], "bar.time") <= _aware(str(first_seen), "first_seen")
            ]
            _extend_active_anchor_extreme(pending_opposite, visible)
            pending_takeover_at = _opposite_anchor_takeover_time(
                background,
                pending_opposite,
                bars=bars,
                through=str(first_seen),
            )
            if pending_takeover_at is not None:
                promoted = deepcopy(pending_opposite)
                promoted["candidate_established_at"] = promoted["first_seen_at"]
                promoted["first_seen_at"] = pending_takeover_at
                promoted["takeover_type"] = "TYPE2"
                promoted["replaced_anchor_ref"] = background.get("id")
                promoted["takeover_reason"] = "OPPOSITE_STRUCTURE_BROKE_ACTIVE_DEFENSE"
                if history_out is not None:
                    historical = deepcopy(background)
                    historical["status"] = "REPLACED"
                    historical["ended_at"] = pending_takeover_at
                    historical["replaced_by"] = promoted["id"]
                    history_out.append(historical)
                background = promoted
                pending_opposite = None
        if background is None or background["direction"] != direction:
            replacement = _new_anchor(
                direction=direction,
                origin=a,
                first_extreme=d,
                defense=c,
                first_seen_at=str(first_seen),
                session_key=session_key,
                level=level,
            )
            if require_closed_confirmation and isinstance(replacement.get("defense"), dict):
                replacement["defense"]["first_seen_at"] = str(first_seen)
            if background is not None and require_closed_confirmation:
                # Preserve the first still-valid opposite campaign and extend
                # it with later same-direction copies.  Its first A/B/C/D may
                # become visible before the current defense breaks; the later
                # break must be able to promote that already-observed campaign
                # without requiring an artificial extra swing.
                if (
                    isinstance(pending_opposite, Mapping)
                    and pending_opposite.get("direction") == replacement.get("direction")
                    and _origin_breach(pending_opposite, bars, through=str(first_seen)) is None
                ):
                    _extend_anchor(pending_opposite, extreme=d, defense=c)
                    if isinstance(pending_opposite.get("defense"), dict):
                        pending_opposite["defense"]["first_seen_at"] = str(first_seen)
                else:
                    pending_opposite = replacement
                takeover_at = _opposite_anchor_takeover_time(
                    background,
                    pending_opposite,
                    bars=bars,
                    through=str(first_seen),
                )
                if takeover_at is None:
                    # A fully formed opposite small structure is still only
                    # the active anchor's correction/reverse candidate while
                    # the current dynasty defense survives.  Recency alone
                    # must not turn it into the new controlling anchor.
                    continue
                replacement = deepcopy(pending_opposite)
                replacement["candidate_established_at"] = replacement["first_seen_at"]
                replacement["first_seen_at"] = takeover_at
                replacement["takeover_type"] = "TYPE2"
                replacement["replaced_anchor_ref"] = background.get("id")
                replacement["takeover_reason"] = "OPPOSITE_STRUCTURE_BROKE_ACTIVE_DEFENSE"
                pending_opposite = None
            if background is not None and history_out is not None:
                historical = deepcopy(background)
                historical["status"] = "REPLACED"
                historical["ended_at"] = replacement["first_seen_at"]
                historical["replaced_by"] = replacement["id"]
                history_out.append(historical)
            background = replacement
        else:
            if require_closed_confirmation and _origin_breach(background, bars, through=str(first_seen)):
                replacement = _new_anchor(direction=direction, origin=a, first_extreme=d,
                    defense=c, first_seen_at=str(first_seen), session_key=session_key, level=level)
                if history_out is not None:
                    old = deepcopy(background)
                    old.update(status="INVALIDATED", ended_at=_origin_breach(background, bars, through=str(first_seen)),
                               replaced_by=replacement["id"])
                    history_out.append(old)
                background = replacement
            else:
                _extend_anchor(background, extreme=d, defense=c)
            if require_closed_confirmation and isinstance(background.get("defense"), dict):
                background["defense"]["first_seen_at"] = str(first_seen)

    if background is None:
        return None

    # A same-direction new extreme extends the existing anchor.  It never
    # creates a new ID and may promote the last intervening opposite pivot to
    # the new defense as long as the original anchor origin remains intact.
    favorable_kind = "HIGH" if background["direction"] == "BULL" else "LOW"
    for index, pivot in enumerate(pivots):
        if (
            pivot["kind"] != favorable_kind
            or _aware(pivot["time"], "pivot.time")
            <= _aware(background["latest_extreme_time"], "anchor.latest_extreme_time")
            or not _more_favorable(
            background["direction"], float(pivot["price"]), float(background["latest_extreme_price"])
            )
        ):
            continue
        preceding = [
            item for item in pivots[:index]
            if item["kind"] != favorable_kind
            and _aware(item["time"], "pivot.time") > _aware(background["latest_extreme_time"], "anchor.latest_extreme_time")
        ]
        defense = preceding[-1] if preceding else None
        if defense is not None and _origin_survives(background, float(defense["price"])):
            qualified_at = None
            if require_closed_confirmation:
                boundary = {
                    "price": background["latest_extreme_price"],
                    "confirmed": True,
                    "confirmation_time": background["first_seen_at"],
                }
                origin = {
                    "price": background["origin_price"],
                    "confirmed": True,
                    "confirmation_time": background["first_seen_at"],
                }
                qualified_at = _closed_anchor_confirmation(
                    (origin, boundary, defense), direction=background["direction"], bars=bars,
                    until=pivots[index + 1]["time"] if index + 1 < len(pivots) else None,
                )
            _extend_anchor(
                background, extreme=pivot,
                defense=defense if not require_closed_confirmation or qualified_at else None,
            )
            if qualified_at is not None:
                background["defense"]["first_seen_at"] = qualified_at
        elif defense is not None and require_closed_confirmation:
            # The intervening opposite extreme violated the old anchor origin.
            # A later attack through the prior same-grade extreme is a NEW
            # destructive impulse, not a resurrection of that terminated anchor.
            boundary = {"time": background["latest_extreme_time"],
                        "price": background["latest_extreme_price"], "confirmed": True,
                        "confirmation_time": background["first_seen_at"]}
            qualified_at = _closed_anchor_confirmation(
                (defense, boundary, defense), direction=background["direction"], bars=bars,
                until=pivots[index+1]["time"] if index+1 < len(pivots) else None)
            breach = _origin_breach(background, bars, through=qualified_at) if qualified_at else None
            if qualified_at and breach:
                replacement = _new_anchor(direction=background["direction"], origin=defense,
                    first_extreme=pivot, defense=None, first_seen_at=qualified_at,
                    session_key=session_key, level=level)
                replacement.update(qualification="REANCHOR_AFTER_ORIGIN_BREACH",
                                   quality_status="REQUIRES_ANALYST_REVIEW")
                if history_out is not None:
                    old = deepcopy(background)
                    old.update(status="INVALIDATED", ended_at=breach, replaced_by=replacement["id"])
                    history_out.append(old)
                background = replacement
        elif defense is None:
            _extend_anchor(background, extreme=pivot, defense=None)

    # The opposite campaign can be established before the current defense is
    # broken and then cross that line without producing another complete
    # A/B/C/D.  Re-evaluate it at the current closed-bar cutoff so takeover is
    # neither lost nor backdated.
    if (
        require_closed_confirmation
        and isinstance(pending_opposite, dict)
        and _origin_breach(pending_opposite, bars) is None
    ):
        _extend_active_anchor_extreme(pending_opposite, bars)
        takeover_at = _opposite_anchor_takeover_time(
            background,
            pending_opposite,
            bars=bars,
            through=str(bars[-1]["time"]),
        )
        if takeover_at is not None:
            replacement = deepcopy(pending_opposite)
            replacement["candidate_established_at"] = replacement["first_seen_at"]
            replacement["first_seen_at"] = takeover_at
            replacement["takeover_type"] = "TYPE2"
            replacement["replaced_anchor_ref"] = background.get("id")
            replacement["takeover_reason"] = "OPPOSITE_STRUCTURE_BROKE_ACTIVE_DEFENSE"
            if history_out is not None:
                historical = deepcopy(background)
                historical["status"] = "REPLACED"
                historical["ended_at"] = takeover_at
                historical["replaced_by"] = replacement["id"]
                history_out.append(historical)
            background = replacement
    if require_closed_confirmation:
        breach = _origin_breach(background, bars)
        if breach:
            background.update(status="INVALIDATED", ended_at=breach, replaced_by=None)
            if history_out is not None:
                history_out.append(deepcopy(background))
            return None
    _refresh_anchor_metrics(background)
    return background


def _opposite_anchor_takeover_time(
    current: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    bars: Sequence[Mapping[str, Any]],
    through: str,
) -> str | None:
    """Require a causal defense break before an opposite dynasty replaces.

    `_derive_directional_anchor` observes every completed A/B/C/D structure.
    That fact alone does not make the newest direction the controlling anchor:
    it may be an orderly correction inside the current dynasty.  Replaying the
    prefix only through the candidate's first-observable close keeps the
    decision causal.  A missing formal defense falls back to the immutable
    anchor origin; otherwise an anchor without a qualified line could block a
    genuine Type-2 takeover forever.
    """

    if candidate.get("direction") == current.get("direction"):
        return str(candidate.get("first_seen_at") or through)
    cutoff = _aware(through, "takeover.through")
    visible = [
        dict(item)
        for item in bars
        if _aware(item["time"], "bar.time") <= cutoff
    ]
    current_at_cutoff = _apply_defense_lifecycle(deepcopy(dict(current)), visible)
    defense = current_at_cutoff.get("defense")
    direction = str(candidate.get("direction") or "")
    extreme = float(candidate.get("latest_extreme_price") or 0)
    if isinstance(defense, Mapping):
        boundary = float(defense["price"])
        break_at = defense.get("broken_at")
    else:
        boundary = float(current_at_cutoff["origin_price"])
        break_at = _origin_breach(current_at_cutoff, visible, through=through)
    crossed = extreme > boundary if direction == "BULL" else extreme < boundary
    if not break_at or not crossed:
        return None
    return max(
        _aware(str(break_at), "takeover.break_at"),
        _aware(str(candidate.get("first_seen_at")), "candidate.first_seen_at"),
    ).isoformat()


def _origin_breach(anchor: Mapping[str, Any], bars: Sequence[Mapping[str, Any]], *, through: str | None = None) -> str | None:
    start = _aware(anchor["first_seen_at"], "anchor.first_seen_at")
    end = _aware(through, "through") if through else None
    for bar in bars:
        at = _aware(bar["time"], "bar.time")
        if at <= start or (end is not None and at > end):
            continue
        if (anchor["direction"] == "BULL" and float(bar["close"]) < float(anchor["origin_price"])) or (
            anchor["direction"] == "BEAR" and float(bar["close"]) > float(anchor["origin_price"])):
            return str(bar["time"])
    return None


def _closed_anchor_confirmation(
    pivots: Sequence[Mapping[str, Any]],
    *,
    direction: str,
    bars: Sequence[Mapping[str, Any]],
    until: str | None,
) -> str | None:
    """First causal close beyond B, never backdated to an earlier wick."""

    if any(not item.get("confirmed") or not item.get("confirmation_time") for item in pivots):
        return None
    known_at = max(_aware(item["confirmation_time"], "pivot.confirmation_time") for item in pivots)
    origin, boundary, correction = pivots
    correction_at = _aware(correction["time"], "correction.time")
    end = _aware(until, "confirmation.until") if until else None
    for bar in bars:
        at = _aware(bar["time"], "bar.time")
        if at < known_at or at <= correction_at or (end is not None and at >= end):
            continue
        close = float(bar["close"])
        if (direction == "BULL" and close <= float(origin["price"])) or (
            direction == "BEAR" and close >= float(origin["price"])
        ):
            return None
        if (direction == "BULL" and close > float(boundary["price"])) or (
            direction == "BEAR" and close < float(boundary["price"])
        ):
            return str(bar["time"])
    return None


def _strictly_increasing_pivot_times(
    pivots: Sequence[Mapping[str, Any]],
) -> bool:
    times = [_aware(item["time"], "pivot.time") for item in pivots]
    return all(left < right for left, right in zip(times, times[1:]))


def _new_anchor(
    *,
    direction: str,
    origin: Mapping[str, Any],
    first_extreme: Mapping[str, Any],
    defense: Mapping[str, Any] | None,
    first_seen_at: str,
    session_key: str,
    level: str,
) -> dict[str, Any]:
    anchor_id = _stable_id("ANCHOR", session_key, level, direction, origin["time"])
    result = {
        "id": anchor_id,
        "record_type": "ANCHOR",
        "level": level,
        "direction": direction,
        "status": "ACTIVE",
        "origin_time": origin["time"],
        "origin_price": float(origin["price"]),
        "first_extreme_time": first_extreme["time"],
        "first_extreme_price": float(first_extreme["price"]),
        "latest_extreme_time": first_extreme["time"],
        "latest_extreme_price": float(first_extreme["price"]),
        "first_seen_at": first_seen_at,
        "defense": (
            _defense_record(defense, anchor_id=anchor_id, direction=direction)
            if isinstance(defense, Mapping)
            else None
        ),
    }
    _refresh_anchor_metrics(result)
    return result


def _extend_anchor(
    anchor: dict[str, Any],
    *,
    extreme: Mapping[str, Any],
    defense: Mapping[str, Any] | None,
) -> None:
    if not _more_favorable(anchor["direction"], float(extreme["price"]), float(anchor["latest_extreme_price"])):
        return
    anchor["latest_extreme_time"] = extreme["time"]
    anchor["latest_extreme_price"] = float(extreme["price"])
    if defense is not None:
        anchor["defense"] = _defense_record(defense, anchor_id=anchor["id"], direction=anchor["direction"])
    _refresh_anchor_metrics(anchor)


def _apply_defense_lifecycle(anchor: dict[str, Any], bars: list[dict[str, Any]]) -> dict[str, Any]:
    defense = anchor.get("defense")
    if not isinstance(defense, dict):
        return anchor
    direction = str(anchor["direction"])
    defense_time = _aware(
        defense.get("first_seen_at", defense["time"]),
        "defense.first_seen_at",
    )
    price = float(defense["price"])
    later = [item for item in bars if _aware(item["time"], "bar.time") > defense_time]
    broken = next(
        (
            item for item in later
            if (direction == "BULL" and float(item["close"]) < price)
            or (direction == "BEAR" and float(item["close"]) > price)
        ),
        None,
    )
    if broken is None:
        return anchor
    defense["state"] = "BROKEN"
    defense["broken_at"] = str(broken["time"])
    broken_time = _aware(broken["time"], "broken.time")
    reclaimed = next(
        (
            item for item in later
            if _aware(item["time"], "bar.time") > broken_time
            and (
                (direction == "BULL" and float(item["close"]) > price)
                or (direction == "BEAR" and float(item["close"]) < price)
            )
        ),
        None,
    )
    anchor["status"] = "DEGRADED"
    if reclaimed is not None:
        defense["state"] = "BROKEN_AND_RECLAIMED"
        defense["reclaimed_at"] = str(reclaimed["time"])
        anchor["status"] = "DEGRADED_RECLAIMED"
    return anchor


def _historical_anchor(anchor: dict[str, Any], bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Freeze terminated roles at their end; later prices cannot reactivate them."""
    result = deepcopy(anchor)
    status = result.get("status", "REPLACED")
    ended_at = result.get("ended_at")
    visible = [b for b in bars if not ended_at or _aware(b["time"], "bar.time") <= _aware(ended_at, "ended_at")]
    result = _apply_defense_lifecycle(result, visible)
    result["status"] = status if status == "INVALIDATED" else "REPLACED"
    return result


def _reverse_candidate(
    background: Mapping[str, Any],
    bars: list[dict[str, Any]],
    *,
    threshold: float,
    session_key: str,
    as_of: datetime,
    require_n2_structure: bool,
) -> dict[str, Any] | None:
    """Return an opposite-direction candidate only after n=2 Dow structure.

    A large counter move is still only a raw correction. Directional-candidate
    status requires three alternating, individually confirmed n=2 pivots
    (A/B/C), followed by a causal closed bar beyond B.  The crossing bar is the
    still-forming D endpoint; requiring D to become a future-confirmed pivot
    would delay an already observable HH+HL or LL+LH relationship.
    """

    if not require_n2_structure:
        return _amplitude_reverse_candidate(
            background,
            bars,
            threshold=threshold,
            session_key=session_key,
        )

    start_time = _aware(background["latest_extreme_time"], "background.latest_extreme_time")
    pivots = [p for p in _confirmed_n2_pivots(bars) if _aware(p["time"], "pivot.time") >= start_time]
    opposite = "BEAR" if background["direction"] == "BULL" else "BULL"
    structures: list[tuple[str, dict[str, Any], dict[str, Any], datetime]] = []
    for index in range(2, len(pivots)):
        a, b, c = pivots[index - 2 : index + 1]
        if not _strictly_increasing_pivot_times((a, b, c)):
            continue
        kinds = [item["kind"] for item in (a, b, c)]
        direction: str | None = None
        if (
            kinds == ["LOW", "HIGH", "LOW"]
            and float(c["price"]) >= float(a["price"])
        ):
            direction = "BULL"
        elif (
            kinds == ["HIGH", "LOW", "HIGH"]
            and float(c["price"]) <= float(a["price"])
        ):
            direction = "BEAR"
        if direction != opposite:
            continue
        first_seen_text = _closed_anchor_confirmation(
            (a, b, c),
            direction=direction,
            bars=bars,
            until=None,
        )
        if first_seen_text is None:
            continue
        first_seen = _aware(first_seen_text, "candidate.first_seen_at")
        if first_seen > as_of:
            continue
        crossing_bar = next(
            (
                bar
                for bar in bars
                if _aware(bar["time"], "bar.time") == first_seen
            ),
            None,
        )
        if crossing_bar is None:
            continue
        extreme_field = "high" if direction == "BULL" else "low"
        forming_d = {
            "kind": "HIGH" if direction == "BULL" else "LOW",
            "time": crossing_bar["time"],
            "price": float(crossing_bar[extreme_field]),
            "confirmed": False,
            "confirmation_time": None,
            "first_seen_at": crossing_bar["time"],
        }
        structures.append((direction, a, forming_d, first_seen))
    # Preserve candidates that only become visible through a later fully
    # confirmed D pivot.  This covers paths where no earlier close beyond B
    # was available when A/B/C first became observable, while the fast path
    # above avoids needlessly waiting for D on paths such as 2026-08-26 09:10.
    for index in range(3, len(pivots)):
        a, b, c, d = pivots[index - 3 : index + 1]
        if not _strictly_increasing_pivot_times((a, b, c, d)):
            continue
        kinds = [item["kind"] for item in (a, b, c, d)]
        direction: str | None = None
        if (
            kinds == ["LOW", "HIGH", "LOW", "HIGH"]
            and float(d["price"]) > float(b["price"])
            and float(c["price"]) >= float(a["price"])
        ):
            direction = "BULL"
        elif (
            kinds == ["HIGH", "LOW", "HIGH", "LOW"]
            and float(d["price"]) < float(b["price"])
            and float(c["price"]) <= float(a["price"])
        ):
            direction = "BEAR"
        if direction != opposite:
            continue
        first_seen = max(
            _aware(item["first_seen_at"], "pivot.first_seen_at")
            for item in (a, b, c, d)
        )
        if first_seen <= as_of:
            structures.append((direction, a, d, first_seen))
    if not structures:
        return None

    structures.sort(
        key=lambda item: (
            item[3],
            _aware(item[1]["time"], "candidate.origin"),
        )
    )

    # A qualified reverse direction is a durable scenario, not a rolling
    # four-pivot window.  Keep the earliest still-valid origin and extend its
    # extreme while the same HH+HL / LL+LH campaign remains alive.  Otherwise
    # every later same-direction pair would silently change the candidate ID
    # and make an existing scenario look newly established.
    selected: tuple[str, dict[str, Any], dict[str, Any], datetime] | None = None
    for structure in structures:
        candidate_direction, candidate_origin, _, _ = structure
        origin_time = _aware(candidate_origin["time"], "candidate.origin")
        origin_price = float(candidate_origin["price"])
        later_closes = [
            float(bar["close"])
            for bar in bars
            if _aware(bar["time"], "bar.time") > origin_time
        ]
        breached = (
            any(close < origin_price for close in later_closes)
            if candidate_direction == "BULL"
            else any(close > origin_price for close in later_closes)
        )
        if not breached:
            selected = structure
            break
    if selected is None:
        return None

    direction, origin, initial_extreme, first_seen = selected
    extreme_kind = "HIGH" if direction == "BULL" else "LOW"
    extreme_field = "high" if direction == "BULL" else "low"
    later_extremes = [
        {
            "kind": extreme_kind,
            "time": bar["time"],
            "price": float(bar[extreme_field]),
            "first_seen_at": bar["time"],
        }
        for bar in bars
        if _aware(bar["time"], "bar.time") >= _aware(initial_extreme["time"], "candidate.extreme")
        and _aware(bar["time"], "bar.time") <= as_of
    ]
    if direction == "BULL":
        extreme = max(later_extremes or [initial_extreme], key=lambda item: float(item["price"]))
    else:
        extreme = min(later_extremes or [initial_extreme], key=lambda item: float(item["price"]))
    amplitude = abs(float(extreme["price"]) - float(origin["price"]))
    defense = background.get("defense") if isinstance(background.get("defense"), Mapping) else {}
    defense_state = str(defense.get("state") or "ACTIVE")
    status = {
        "ACTIVE": "QUALIFIED",
        "BROKEN": "DEFENSE_BREAK",
        "BROKEN_AND_RECLAIMED": "AWAITING_CONTINUATION",
    }.get(defense_state, "QUALIFIED")
    result = {
        "id": _stable_id("REVERSE", session_key, direction, origin["time"]),
        "record_type": "REVERSE_CANDIDATE",
        "level": "SMALL",
        "direction": direction,
        "status": status,
        "start_time": origin["time"],
        "start_price": float(origin["price"]),
        "current_extreme_time": extreme["time"],
        "current_extreme_price": float(extreme["price"]),
        "amplitude_points": round(amplitude, 4),
        "duration_minutes": _minutes(
            _aware(origin["time"], "candidate.origin"),
            _aware(extreme["time"], "candidate.extreme"),
        ),
        "first_seen_at": first_seen.isoformat(),
        "takeover_condition": "PULLBACK_THEN_LOWER_LOW" if direction == "BEAR" else "PULLBACK_THEN_HIGHER_HIGH",
        "takeover_level": float(extreme["price"]),
        "replaces_background": False,
    }
    return result


def _amplitude_reverse_candidate(
    background: Mapping[str, Any],
    bars: list[dict[str, Any]],
    *,
    threshold: float,
    session_key: str,
) -> dict[str, Any] | None:
    """Retain the pre-v9 amplitude candidate for old replay contracts."""

    start_time = _aware(background["latest_extreme_time"], "background.latest_extreme_time")
    later = [item for item in bars if _aware(item["time"], "bar.time") > start_time]
    if not later:
        return None
    if background["direction"] == "BULL":
        extreme_price = min(float(item["low"]) for item in later)
        extreme_bar = [item for item in later if float(item["low"]) == extreme_price][-1]
        direction = "BEAR"
        amplitude = float(background["latest_extreme_price"]) - extreme_price
    else:
        extreme_price = max(float(item["high"]) for item in later)
        extreme_bar = [item for item in later if float(item["high"]) == extreme_price][-1]
        direction = "BULL"
        amplitude = extreme_price - float(background["latest_extreme_price"])
    if amplitude < threshold:
        return None
    first_seen_bar = next(
        item for item in later
        if (
            direction == "BEAR"
            and float(background["latest_extreme_price"]) - float(item["low"]) >= threshold
        )
        or (
            direction == "BULL"
            and float(item["high"]) - float(background["latest_extreme_price"]) >= threshold
        )
    )
    defense = background.get("defense") if isinstance(background.get("defense"), Mapping) else {}
    defense_state = str(defense.get("state") or "ACTIVE")
    status = {
        "ACTIVE": "QUALIFIED",
        "BROKEN": "DEFENSE_BREAK",
        "BROKEN_AND_RECLAIMED": "AWAITING_CONTINUATION",
    }.get(defense_state, "QUALIFIED")
    return {
        "id": _stable_id("REVERSE", session_key, direction, background["latest_extreme_time"]),
        "record_type": "REVERSE_CANDIDATE",
        "level": "LARGE",
        "direction": direction,
        "status": status,
        "start_time": background["latest_extreme_time"],
        "start_price": float(background["latest_extreme_price"]),
        "current_extreme_time": str(extreme_bar["time"]),
        "current_extreme_price": extreme_price,
        "amplitude_points": round(amplitude, 4),
        "duration_minutes": _minutes(start_time, _aware(extreme_bar["time"], "extreme.time")),
        "first_seen_at": str(first_seen_bar["time"]),
        "takeover_condition": "PULLBACK_THEN_LOWER_LOW" if direction == "BEAR" else "PULLBACK_THEN_HIGHER_HIGH",
        "takeover_level": extreme_price,
        "replaces_background": False,
    }


def _working_leg(
    background: Mapping[str, Any],
    candidate: Mapping[str, Any] | None,
    bars: list[dict[str, Any]],
    *,
    session_key: str,
    as_of: datetime,
    threshold: float,
    strict_course_pivots: bool,
) -> dict[str, Any] | None:
    if candidate is None:
        if not strict_course_pivots:
            return _legacy_background_working_leg(
                background,
                bars,
                session_key=session_key,
                as_of=as_of,
            )
        return _raw_working_leg(
            background,
            bars,
            session_key=session_key,
            as_of=as_of,
            threshold=threshold,
        )

    if strict_course_pivots:
        # A candidate's recognition time is not the start of its pullback.
        # Keep the full revealed prefix, including pivots confirmed just
        # before recognition, and use the same causal leg builder as the
        # background-only path. Otherwise e.g. a 09:15 low is lost when the
        # candidate becomes observable at 09:16.
        working = _raw_working_leg(
            background, bars, session_key=session_key, as_of=as_of, threshold=threshold,
        )
        if working is not None:
            if working["direction"] != candidate["direction"]:
                working["role"] = "REVERSE_CANDIDATE_PULLBACK"
            elif _aware(working["start_time"], "working.start") > _aware(
                candidate["current_extreme_time"], "candidate.extreme"
            ):
                working["role"] = "REVERSE_CANDIDATE_CONTINUATION"
            else:
                working["role"] = "REVERSE_CANDIDATE_IMPULSE"
            working["id"] = _stable_id(
                "WORKING", session_key, working["direction"], working["start_time"], working["role"],
            )
        return working

    candidate_seen = _aware(candidate["first_seen_at"], "candidate.first_seen_at")
    revealed_after_candidate = [item for item in bars if _aware(item["time"], "bar.time") > candidate_seen]
    if background.get("anchor_origin_kind") == "SESSION_OPEN" and revealed_after_candidate:
        if candidate["direction"] == "BULL":
            pullback_price = min(float(item["low"]) for item in revealed_after_candidate)
            pullback_bar = next(item for item in revealed_after_candidate if float(item["low"]) == pullback_price)
            continuation = [item for item in revealed_after_candidate if _aware(item["time"], "bar.time") > _aware(pullback_bar["time"], "pullback.time")]
            if continuation:
                end_price = max(float(item["high"]) for item in continuation)
                end_bar = [item for item in continuation if float(item["high"]) == end_price][-1]
                return _working_record(
                    session_key=session_key,
                    direction="BULL",
                    start_time=_aware(pullback_bar["time"], "working.start"),
                    start_price=pullback_price,
                    end_time=_aware(end_bar["time"], "working.end"),
                    end_price=end_price,
                    level="SMALL",
                    role="REVERSE_CANDIDATE_CONTINUATION",
                    as_of=as_of,
                )
        else:
            pullback_price = max(float(item["high"]) for item in revealed_after_candidate)
            pullback_bar = next(item for item in revealed_after_candidate if float(item["high"]) == pullback_price)
            continuation = [item for item in revealed_after_candidate if _aware(item["time"], "bar.time") > _aware(pullback_bar["time"], "pullback.time")]
            if continuation:
                end_price = min(float(item["low"]) for item in continuation)
                end_bar = [item for item in continuation if float(item["low"]) == end_price][-1]
                return _working_record(
                    session_key=session_key,
                    direction="BEAR",
                    start_time=_aware(pullback_bar["time"], "working.start"),
                    start_price=pullback_price,
                    end_time=_aware(end_bar["time"], "working.end"),
                    end_price=end_price,
                    level="SMALL",
                    role="REVERSE_CANDIDATE_CONTINUATION",
                    as_of=as_of,
                )

    candidate_extreme_time = _aware(candidate["current_extreme_time"], "candidate.current_extreme_time")
    after_extreme = [item for item in bars if _aware(item["time"], "bar.time") > candidate_extreme_time]
    if after_extreme:
        if candidate["direction"] == "BEAR":
            end_price = max(float(item["high"]) for item in after_extreme)
            end_bar = next(item for item in after_extreme if float(item["high"]) == end_price)
            direction = "BULL"
        else:
            end_price = min(float(item["low"]) for item in after_extreme)
            end_bar = next(item for item in after_extreme if float(item["low"]) == end_price)
            direction = "BEAR"
        return _working_record(
            session_key=session_key,
            direction=direction,
            start_time=candidate_extreme_time,
            start_price=float(candidate["current_extreme_price"]),
            end_time=_aware(end_bar["time"], "working.end"),
            end_price=end_price,
            level="SMALL",
            role="REVERSE_CANDIDATE_PULLBACK",
            as_of=as_of,
        )
    return _working_record(
        session_key=session_key,
        direction=str(candidate["direction"]),
        start_time=_aware(candidate["start_time"], "candidate.start_time"),
        start_price=float(candidate["start_price"]),
        end_time=candidate_extreme_time,
        end_price=float(candidate["current_extreme_price"]),
        level="LARGE",
        role="REVERSE_CANDIDATE_IMPULSE",
        as_of=as_of,
    )


def _legacy_background_working_leg(
    background: Mapping[str, Any],
    bars: list[dict[str, Any]],
    *,
    session_key: str,
    as_of: datetime,
) -> dict[str, Any] | None:
    start_time = _aware(background["latest_extreme_time"], "background.latest_extreme_time")
    start_price = float(background["latest_extreme_price"])
    later = [item for item in bars if _aware(item["time"], "bar.time") > start_time]
    if not later:
        return None
    if background["direction"] == "BULL":
        extreme_price = min(float(item["low"]) for item in later)
        extreme_bar = next(item for item in later if float(item["low"]) == extreme_price)
        direction = "BEAR"
    else:
        extreme_price = max(float(item["high"]) for item in later)
        extreme_bar = next(item for item in later if float(item["high"]) == extreme_price)
        direction = "BULL"
    return _working_record(
        session_key=session_key,
        direction=direction,
        start_time=start_time,
        start_price=start_price,
        end_time=_aware(extreme_bar["time"], "working.end"),
        end_price=extreme_price,
        level="LARGE",
        role="BACKGROUND_CORRECTION",
        as_of=as_of,
    )


def _raw_working_leg(
    background: Mapping[str, Any],
    bars: list[dict[str, Any]],
    *,
    session_key: str,
    as_of: datetime,
    threshold: float,
) -> dict[str, Any] | None:
    """Describe the latest raw swing without granting directional status."""

    anchor_time = _aware(
        background.get("first_extreme_time", background["latest_extreme_time"]),
        "background.latest_extreme_time",
    )
    scoped = [item for item in bars if _aware(item["time"], "bar.time") > anchor_time]
    if not scoped:
        return None

    # Once a course n=2 pivot has been confirmed, it is the causal start of
    # the current swing.  Do not let the threshold zigzag replace that start
    # with a nearby candle merely because the latest bars contain overlapping
    # highs and lows.  In particular, a still-unconfirmed bounce after a new
    # low remains part of the down leg until a same-grade low pivot exists.
    confirmed_pivots = [
        item
        for item in _confirmed_n2_pivots(bars)
        if _aware(item["time"], "pivot.time") > anchor_time
        and _aware(item["first_seen_at"], "pivot.first_seen_at") <= as_of
    ]
    if confirmed_pivots:
        start = confirmed_pivots[-1]
        start_index = len(confirmed_pivots) - 1
        start_time = _aware(start["time"], "working.start")
        later = [item for item in bars if _aware(item["time"], "bar.time") > start_time]
        if later:
            broken_forward = (
                start["kind"] == "HIGH"
                and any(float(item["close"]) > float(start["price"]) for item in later)
            ) or (
                start["kind"] == "LOW"
                and any(float(item["close"]) < float(start["price"]) for item in later)
            )
            if broken_forward:
                opposite = "LOW" if start["kind"] == "HIGH" else "HIGH"
                prior = next(
                    (
                        item
                        for item in reversed(confirmed_pivots[:start_index])
                        if item.get("kind") == opposite
                    ),
                    None,
                )
                if prior is not None:
                    start = prior
                    start_time = _aware(start["time"], "working.start")
                    later = [
                        item for item in bars
                        if _aware(item["time"], "bar.time") > start_time
                    ]
        if later:
            if start["kind"] == "HIGH":
                end_price = min(float(item["low"]) for item in later)
                end_bar = next(item for item in later if float(item["low"]) == end_price)
                direction = "BEAR"
            else:
                end_price = max(float(item["high"]) for item in later)
                end_bar = next(item for item in later if float(item["high"]) == end_price)
                direction = "BULL"
            role = (
                "BACKGROUND_RETEST"
                if direction == background.get("direction")
                else "BACKGROUND_CORRECTION"
            )
            return _working_record(
                session_key=session_key,
                direction=direction,
                start_time=start_time,
                start_price=float(start["price"]),
                end_time=_aware(end_bar["time"], "working.end"),
                end_price=end_price,
                level="SMALL",
                role=role,
                as_of=as_of,
            )

    turns = _course_pivots(scoped, level="SMALL")
    selected: tuple[Mapping[str, Any], Mapping[str, Any]] | None = None
    for left, right in reversed(list(zip(turns, turns[1:]))):
        if (
            left.get("kind") != right.get("kind")
            and _aware(left["time"], "working.left")
            < _aware(right["time"], "working.right")
        ):
            selected = (left, right)
            break
    if selected is None:
        if background.get("direction") == "BULL":
            end_price = min(float(item["low"]) for item in scoped)
            end_bar = next(item for item in scoped if float(item["low"]) == end_price)
            end_kind = "LOW"
        else:
            end_price = max(float(item["high"]) for item in scoped)
            end_bar = next(item for item in scoped if float(item["high"]) == end_price)
            end_kind = "HIGH"
        start = {
            "time": background.get("first_extreme_time", background["latest_extreme_time"]),
            "price": float(background.get("first_extreme_price", background["latest_extreme_price"])),
            "kind": "HIGH" if background.get("direction") == "BULL" else "LOW",
        }
        end = {"time": end_bar["time"], "price": end_price, "kind": end_kind}
    else:
        start, end = selected
    direction = "BULL" if end["kind"] == "HIGH" else "BEAR"
    role = (
        "BACKGROUND_RETEST"
        if direction == background.get("direction")
        else "BACKGROUND_CORRECTION"
    )
    return _working_record(
        session_key=session_key,
        direction=direction,
        start_time=_aware(start["time"], "working.start"),
        start_price=float(start["price"]),
        end_time=_aware(end["time"], "working.end"),
        end_price=float(end["price"]),
        level="SMALL",
        role=role,
        as_of=as_of,
    )


def _working_record(
    *,
    session_key: str,
    direction: str,
    start_time: datetime,
    start_price: float,
    end_time: datetime,
    end_price: float,
    level: str,
    role: str,
    as_of: datetime,
) -> dict[str, Any]:
    return {
        "id": _stable_id("WORKING", session_key, direction, start_time.isoformat(), role),
        "record_type": "WORKING_LEG",
        "level": level,
        "direction": direction,
        "status": "FORMING",
        "role": role,
        "start_time": start_time.isoformat(),
        "start_price": float(start_price),
        "current_extreme_time": end_time.isoformat(),
        "current_extreme_price": float(end_price),
        "amplitude_points": round(end_price - start_price, 4),
        "duration_minutes": _minutes(start_time, end_time),
        "first_seen_at": start_time.isoformat(),
        "updated_at": as_of.isoformat(),
    }


def _unclassified_working_leg(
    pivots: list[dict[str, Any]],
    *,
    session_key: str,
    level: str,
    as_of: datetime,
) -> dict[str, Any] | None:
    if len(pivots) < 2:
        return None
    start, end = pivots[-2], pivots[-1]
    direction = "BULL" if float(end["price"]) > float(start["price"]) else "BEAR"
    return _working_record(
        session_key=session_key,
        direction=direction,
        start_time=_aware(start["time"], "start.time"),
        start_price=float(start["price"]),
        end_time=_aware(end["time"], "end.time"),
        end_price=float(end["price"]),
        level=level,
        role="UNCLASSIFIED",
        as_of=as_of,
    )


def _lifecycle_events(
    *,
    background: Mapping[str, Any] | None,
    child: Mapping[str, Any] | None,
    candidate: Mapping[str, Any] | None,
    history: list[dict[str, Any]],
    session_key: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for old in history:
        events.append(
            _event(
                "BACKGROUND_ANCHOR_INVALIDATED" if old.get("status") == "INVALIDATED" else "BACKGROUND_ANCHOR_REPLACED",
                str(old.get("ended_at")),
                session_key,
                anchor_ref=old.get("id"),
                replacement_ref=old.get("replaced_by"),
                direction=old.get("direction"),
            )
        )
        old_defense = old.get("defense") if isinstance(old.get("defense"), Mapping) else None
        if old_defense and old_defense.get("broken_at"):
            events.append(
                _event(
                    "BACKGROUND_DEFENSE_BROKEN",
                    str(old_defense["broken_at"]),
                    session_key,
                    anchor_ref=old.get("id"),
                    defense_ref=old_defense.get("id"),
                    direction=old.get("direction"),
                )
            )
    defense = None
    if isinstance(background, Mapping):
        events.append(
            _event(
                "BACKGROUND_ANCHOR_ESTABLISHED",
                str(background["first_seen_at"]),
                session_key,
                anchor_ref=background["id"],
                direction=background["direction"],
            )
        )
        defense = background.get("defense") if isinstance(background.get("defense"), Mapping) else None
    if defense and defense.get("broken_at"):
        events.append(
            _event(
                "BACKGROUND_DEFENSE_BROKEN",
                str(defense["broken_at"]),
                session_key,
                anchor_ref=background["id"],
                defense_ref=defense["id"],
                direction=background["direction"],
            )
        )
    if defense and defense.get("reclaimed_at"):
        events.append(
            _event(
                "BACKGROUND_DEFENSE_RECLAIMED",
                str(defense["reclaimed_at"]),
                session_key,
                anchor_ref=background["id"],
                defense_ref=defense["id"],
                direction=background["direction"],
            )
        )
    if candidate is not None:
        events.append(
            _event(
                "REVERSE_CANDIDATE_ESTABLISHED",
                str(candidate["first_seen_at"]),
                session_key,
                candidate_ref=candidate["id"],
                direction=candidate["direction"],
            )
        )
    if child is not None:
        events.append(
            _event(
                "CHILD_ANCHOR_ESTABLISHED",
                str(child["first_seen_at"]),
                session_key,
                anchor_ref=child["id"],
                direction=child["direction"],
            )
        )
    return sorted(events, key=lambda item: (item["event_time"], item["id"]))


def _event(event_type: str, event_time: str, session_key: str, **details: Any) -> dict[str, Any]:
    return {
        "id": _stable_id("ANCHOR_EVENT", session_key, event_type, event_time, *details.values()),
        "event_type": event_type,
        "event_time": event_time,
        "first_seen_at": event_time,
        **details,
    }


def _defense_record(pivot: Mapping[str, Any], *, anchor_id: str, direction: str) -> dict[str, Any]:
    return {
        "id": _stable_id("ANCHOR_DEFENSE", anchor_id, pivot["time"]),
        "direction": direction,
        "time": pivot["time"],
        "price": float(pivot["price"]),
        "state": "ACTIVE",
        "broken_at": None,
        "reclaimed_at": None,
    }


def _refresh_anchor_metrics(anchor: dict[str, Any]) -> None:
    origin_time = _aware(anchor["origin_time"], "anchor.origin_time")
    extreme_time = _aware(anchor["latest_extreme_time"], "anchor.latest_extreme_time")
    anchor["amplitude_points"] = round(float(anchor["latest_extreme_price"]) - float(anchor["origin_price"]), 4)
    anchor["duration_minutes"] = _minutes(origin_time, extreme_time)


def _origin_survives(anchor: Mapping[str, Any], price: float) -> bool:
    return price > float(anchor["origin_price"]) if anchor["direction"] == "BULL" else price < float(anchor["origin_price"])


def _more_favorable(direction: str, candidate: float, current: float) -> bool:
    return candidate > current if direction == "BULL" else candidate < current


def _point(kind: str, bar: Mapping[str, Any], price: float, *, confirmed: bool) -> dict[str, Any]:
    return {
        "kind": kind,
        "time": _aware(bar["time"], "bar.time").isoformat(),
        "price": float(price),
        "confirmed": bool(confirmed),
        "confirmation_time": None,
    }


def _confirm(point: Mapping[str, Any], *, at: datetime) -> dict[str, Any]:
    return {**point, "confirmed": True, "confirmation_time": at.isoformat()}


def _dedupe_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in turns:
        if result and result[-1]["kind"] == item["kind"]:
            if _more_favorable("BULL" if item["kind"] == "HIGH" else "BEAR", float(item["price"]), float(result[-1]["price"])):
                result[-1] = item
            continue
        result.append(item)
    return result


def _normalize_bars(bars: Sequence[Mapping[str, Any]], *, expected: datetime) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in bars:
        at = _aware(raw.get("time"), "bar.time")
        if at > expected:
            raise AnchorLifecycleError("錨生命週期收到未來K棒。")
        try:
            item = {
                "time": at.isoformat(),
                "open": float(raw["open"]),
                "high": float(raw["high"]),
                "low": float(raw["low"]),
                "close": float(raw["close"]),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise AnchorLifecycleError("錨生命週期K棒格式無效。") from exc
        if item["low"] > item["high"] or not item["low"] <= item["open"] <= item["high"] or not item["low"] <= item["close"] <= item["high"]:
            raise AnchorLifecycleError("錨生命週期K棒OHLC關係無效。")
        result.append(item)
    result.sort(key=lambda item: item["time"])
    return result


def _minutes(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() // 60))


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = "|".join(str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _aware(value: Any, field: str) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise AnchorLifecycleError(f"{field}不是有效時間。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AnchorLifecycleError(f"{field}必須含時區。")
    return parsed
