"""FINAL Candidate 2 deterministic objective-state primitives.

This module deliberately uses the already-frozen V1 pivot facts as a
compatibility baseline.  Those radius-3/radius-10 facts are *not* asserted to
be the course L1/L2 definition.  The module performs no I/O, no AI judgement,
and no trade approval.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date
from typing import Any, Iterable, Mapping, Sequence


ENGINE_VERSION = "hybrid-v3-objective-state-v3"
ENGINE_STATUS = "FINAL"
PIVOT_DEFINITION = "LEGACY_RADIUS_3_10_NOT_COURSE_L1_L2"
EXPERIMENTAL_COURSE_PIVOT_DEFINITION = "COURSE_CAUSAL_L1_L2_EXPERIMENTAL_DISABLED"
SCALES = ("SMALL", "LARGE")
SIDES = ("HIGH", "LOW")


def _day(value: Any) -> str:
    text = str(value)
    date.fromisoformat(text)
    return text


def _canonical_sha(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cmp(left: float, right: float) -> int:
    tolerance = max(1e-9, max(abs(left), abs(right)) * 1e-12)
    if left > right + tolerance:
        return 1
    if left < right - tolerance:
        return -1
    return 0


def _pivot_ref(row: Mapping[str, Any]) -> str:
    return str(row.get("ref") or "PIVOT:{scale}:{side}:{source_date}:{confirmation_date}".format(**row))


def _normalize_bars(as_of: str, bars: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in bars:
        row = dict(raw)
        day = _day(row["date"])
        if day > as_of:
            raise ValueError(f"future bar is forbidden: {day} > {as_of}")
        if day in seen:
            raise ValueError(f"duplicate bar date: {day}")
        seen.add(day)
        close = float(row["close"])
        output.append({**row, "date": day, "close": close})
    return sorted(output, key=lambda row: row["date"])


def _normalize_pivots(as_of: str, pivots: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for raw in pivots:
        row = dict(raw)
        scale = str(row["scale"]).upper()
        side = str(row["side"]).upper()
        if scale not in SCALES or side not in SIDES:
            raise ValueError(f"unsupported pivot scale/side: {scale}/{side}")
        source = _day(row["source_date"])
        confirmed = _day(row["confirmation_date"])
        if source > confirmed:
            raise ValueError(f"pivot source is after confirmation: {source}/{confirmed}")
        if confirmed > as_of:
            raise ValueError(f"future-confirmed pivot is forbidden: {confirmed} > {as_of}")
        normalized = {
            **row,
            "scale": scale,
            "side": side,
            "source_date": source,
            "confirmation_date": confirmed,
            "price": float(row["price"]),
            "pivot_definition": PIVOT_DEFINITION,
        }
        ref = _pivot_ref(normalized)
        normalized["ref"] = ref
        if ref in output and output[ref] != normalized:
            raise ValueError(f"conflicting duplicate pivot: {ref}")
        output[ref] = normalized
    return sorted(
        output.values(),
        key=lambda row: (row["confirmation_date"], row["source_date"], row["scale"], row["side"], row["ref"]),
    )


def _relations_for_scale(pivots: Sequence[Mapping[str, Any]], scale: str) -> dict[str, Any]:
    selected = [row for row in pivots if row["scale"] == scale]
    by_side = {
        side: sorted(
            (row for row in selected if row["side"] == side),
            key=lambda row: (row["source_date"], row["confirmation_date"], row["ref"]),
        )
        for side in SIDES
    }

    def relation(side: str) -> tuple[str, list[str]]:
        rows = by_side[side]
        if len(rows) < 2:
            return "INSUFFICIENT", [row["ref"] for row in rows]
        prior, current = rows[-2:]
        comparison = _cmp(float(current["price"]), float(prior["price"]))
        if side == "HIGH":
            label = "HH" if comparison > 0 else "LH" if comparison < 0 else "EH"
        else:
            label = "HL" if comparison > 0 else "LL" if comparison < 0 else "EL"
        return label, [prior["ref"], current["ref"]]

    high_relation, high_refs = relation("HIGH")
    low_relation, low_refs = relation("LOW")
    if high_relation == "HH" and low_relation in {"HL", "EL"}:
        dow = "BULL"
    elif low_relation == "LL" and high_relation in {"LH", "EH"}:
        dow = "BEAR"
    elif "INSUFFICIENT" in {high_relation, low_relation}:
        dow = "UNDEFINED"
    else:
        dow = "TRANSITION"
    return {
        "scale": scale,
        "high_relation": high_relation,
        "low_relation": low_relation,
        "dow_state": dow,
        "direction": {"BULL": "UP", "BEAR": "DOWN", "TRANSITION": "MIXED"}.get(dow, "UNRESOLVED"),
        "evidence_refs": sorted(set(high_refs + low_refs)),
        "method": "LAST_TWO_CONFIRMED_LEGACY_PIVOTS_PER_SIDE",
    }


def derive_dow_states(confirmed_pivots: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Derive HH/HL/LL/LH and Dow states from already-confirmed legacy pivots."""
    return {
        "pivot_definition": PIVOT_DEFINITION,
        "scales": {scale.lower(): _relations_for_scale(confirmed_pivots, scale) for scale in SCALES},
    }


def _latest_control(
    pivots: Sequence[Mapping[str, Any]], *, scale: str, side: str, before: str
) -> dict[str, Any] | None:
    eligible = [
        row
        for row in pivots
        if row["scale"] == scale and row["side"] == side and row["confirmation_date"] < before
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda row: (row["source_date"], row["confirmation_date"], row["ref"]))


def _attack_origin(
    pivots: Sequence[Mapping[str, Any]], *, scale: str, side: str, control: Mapping[str, Any], before: str
) -> dict[str, Any] | None:
    eligible = [
        row
        for row in pivots
        if row["scale"] == scale
        and row["side"] == side
        and row["confirmation_date"] < before
        and control["source_date"] < row["source_date"] < before
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda row: (row["source_date"], row["confirmation_date"], row["ref"]))


def derive_attacks_and_defenses(
    *, bars: Sequence[Mapping[str, Any]], confirmed_pivots: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Replay causal close-cross attacks and defenses from legacy facts.

    A pivot confirmed on day T is intentionally ineligible as a control on T.
    It can first be crossed on a later supplied trading day.
    """
    attacks: list[dict[str, Any]] = []
    defenses: list[dict[str, Any]] = []
    defense_events: list[dict[str, Any]] = []
    active: dict[tuple[str, str], dict[str, Any] | None] = defaultdict(lambda: None)
    campaign_count: dict[tuple[str, str], int] = defaultdict(int)
    consumed: set[tuple[str, str]] = set()

    for index, bar in enumerate(bars):
        if index == 0:
            continue
        day = str(bar["date"])
        close = float(bar["close"])
        previous_close = float(bars[index - 1]["close"])

        # Existing defenses can fail on this close; defenses established below
        # are not tested until a later bar.
        for key, current in list(active.items()):
            if current is None:
                continue
            breached = close < current["price"] if current["side"] == "BULLISH" else close > current["price"]
            if breached:
                current["status"] = "BREACHED"
                current["breached_on"] = day
                active[key] = None
                defense_events.append({"event": "DEFENSE_BREACHED", "date": day, "defense_id": current["defense_id"]})

        day_attacks: list[dict[str, Any]] = []
        for scale in SCALES:
            for direction, control_side, origin_side in (
                ("UP", "HIGH", "LOW"),
                ("DOWN", "LOW", "HIGH"),
            ):
                control = _latest_control(confirmed_pivots, scale=scale, side=control_side, before=day)
                if control is None or (scale, control["ref"]) in consumed:
                    continue
                level = float(control["price"])
                crossed = (
                    _cmp(previous_close, level) <= 0 and _cmp(close, level) > 0
                    if direction == "UP"
                    else _cmp(previous_close, level) >= 0 and _cmp(close, level) < 0
                )
                if not crossed:
                    continue
                consumed.add((scale, control["ref"]))
                origin = _attack_origin(
                    confirmed_pivots, scale=scale, side=origin_side, control=control, before=day
                )
                attack = {
                    "attack_id": f"ATTACK:{scale}:{direction}:{day}:{control['ref']}",
                    "scale": scale,
                    "direction": direction,
                    "confirmed_on": day,
                    "control_pivot_ref": control["ref"],
                    "control_price": level,
                    "origin_pivot_ref": None if origin is None else origin["ref"],
                    "origin_price": None if origin is None else float(origin["price"]),
                    "pivot_definition": PIVOT_DEFINITION,
                    "intrabar_order_unknown": False,
                }
                day_attacks.append(attack)

                if origin is None:
                    attack["defense_result"] = "NO_CAUSAL_ORIGIN"
                    continue
                defense_side = "BULLISH" if direction == "UP" else "BEARISH"
                key = (scale, defense_side)
                current = active[key]
                candidate_price = float(origin["price"])
                improves = current is None or (
                    candidate_price > current["price"] if defense_side == "BULLISH" else candidate_price < current["price"]
                )
                equal = current is not None and _cmp(candidate_price, float(current["price"])) == 0
                if current is not None and (not improves or equal):
                    attack["defense_result"] = "REJECTED_NON_MONOTONIC"
                    defense_events.append(
                        {
                            "event": "DEFENSE_CANDIDATE_REJECTED",
                            "date": day,
                            "attack_id": attack["attack_id"],
                            "reason": "NON_MONOTONIC_OR_EQUAL",
                        }
                    )
                    continue
                supersedes = None
                if current is not None:
                    supersedes = current["defense_id"]
                    current["status"] = "SUPERSEDED"
                    current["superseded_on"] = day
                else:
                    campaign_count[key] += 1
                campaign_id = f"CAMPAIGN:{scale}:{defense_side}:{campaign_count[key]}"
                defense = {
                    "defense_id": f"DEFENSE:{scale}:{defense_side}:{day}:{origin['ref']}",
                    "campaign_id": campaign_id,
                    "scale": scale,
                    "side": defense_side,
                    "price": candidate_price,
                    "source_pivot_ref": origin["ref"],
                    "control_pivot_ref": control["ref"],
                    "established_on": day,
                    "status": "ACTIVE",
                    "breached_on": None,
                    "supersedes_defense_id": supersedes,
                    "pivot_definition": PIVOT_DEFINITION,
                }
                defenses.append(defense)
                active[key] = defense
                attack["defense_result"] = "ESTABLISHED"
                attack["defense_id"] = defense["defense_id"]
                defense_events.append({"event": "DEFENSE_ESTABLISHED", "date": day, "defense_id": defense["defense_id"]})

        directions = {row["direction"] for row in day_attacks}
        if len(directions) > 1:
            for row in day_attacks:
                row["intrabar_order_unknown"] = True
        attacks.extend(day_attacks)

    run_number = 0
    prior_direction: str | None = None
    for attack in sorted(attacks, key=lambda row: (row["confirmed_on"], row["direction"], row["scale"])):
        if attack["intrabar_order_unknown"]:
            attack["directional_run_id"] = f"RUN:AMBIGUOUS:{attack['confirmed_on']}"
            prior_direction = None
        else:
            if attack["direction"] != prior_direction:
                run_number += 1
                prior_direction = attack["direction"]
            attack["directional_run_id"] = f"RUN:{run_number}:{attack['direction']}"

    controls: dict[str, Any] = {}
    for scale in SCALES:
        rows = [row for row in attacks if row["scale"] == scale]
        if not rows:
            controls[scale.lower()] = {"state": "UNRESOLVED", "attack_id": None}
            continue
        latest_day = max(row["confirmed_on"] for row in rows)
        latest = [row for row in rows if row["confirmed_on"] == latest_day]
        directions = {row["direction"] for row in latest}
        if len(directions) != 1 or any(row["intrabar_order_unknown"] for row in latest):
            controls[scale.lower()] = {
                "state": "CONTESTED",
                "attack_id": None,
                "reason": "LATEST_ATTACK_DIRECTION_OR_INTRABAR_ORDER_CONFLICT",
            }
        else:
            chosen = latest[-1]
            defense_side = "BULLISH" if chosen["direction"] == "UP" else "BEARISH"
            supporting_defense = active.get((scale, defense_side))
            if supporting_defense is None:
                controls[scale.lower()] = {
                    "state": "CONTESTED",
                    "attack_id": chosen["attack_id"],
                    "confirmed_on": chosen["confirmed_on"],
                    "reason": "LATEST_ATTACK_HAS_NO_ACTIVE_CAUSAL_DEFENSE",
                }
            else:
                controls[scale.lower()] = {
                    "state": "UP_CONTROL" if chosen["direction"] == "UP" else "DOWN_CONTROL",
                    "attack_id": chosen["attack_id"],
                    "confirmed_on": chosen["confirmed_on"],
                    "active_defense_id": supporting_defense["defense_id"],
                }
    return {
        "pivot_definition": PIVOT_DEFINITION,
        "attacks": attacks,
        "defenses": defenses,
        "defense_events": defense_events,
        "controls": controls,
    }


def derive_scale_relationship(controls: Mapping[str, Any]) -> dict[str, str]:
    large = str((controls.get("large") or {}).get("state") or "UNRESOLVED")
    small = str((controls.get("small") or {}).get("state") or "UNRESOLVED")
    if large == small and large in {"UP_CONTROL", "DOWN_CONTROL"}:
        relationship = "DOW_DIRECTION_RESONANCE"
    elif large == "UP_CONTROL" and small == "DOWN_CONTROL":
        relationship = "SMALL_COUNTER_LARGE_UP"
    elif large == "DOWN_CONTROL" and small == "UP_CONTROL":
        relationship = "SMALL_COUNTER_LARGE_DOWN"
    else:
        relationship = "UNRESOLVED"
    return {"pivot_definition": PIVOT_DEFINITION, "relationship": relationship}


def derive_left_right_state(
    *,
    as_of: str,
    bars: Sequence[Mapping[str, Any]],
    confirmed_pivots: Sequence[Mapping[str, Any]],
    dow_states: Mapping[str, Any],
    attacks_and_defenses: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive the permission-relevant long reversal phase conservatively."""
    defenses = [
        row
        for row in attacks_and_defenses["defenses"]
        if row["scale"] == "LARGE" and row["side"] == "BEARISH"
    ]
    if not defenses:
        phase = "LEFT_PRECONFIRM" if dow_states["large"]["dow_state"] == "BEAR" else "NONE"
        return {
            "pivot_definition": PIVOT_DEFINITION,
            "phase": phase,
            "boundary_defense_ref": None,
            "legacy_proxy": True,
        }
    defense = max(defenses, key=lambda row: (row["established_on"], row["defense_id"]))
    start = defense["established_on"]
    small_up = [
        row
        for row in attacks_and_defenses["attacks"]
        if row["scale"] == "SMALL" and row["direction"] == "UP" and row["confirmed_on"] >= start
    ]
    if defense["status"] == "ACTIVE":
        return {
            "pivot_definition": PIVOT_DEFINITION,
            "phase": "LR" if small_up else "LEFT_PRECONFIRM",
            "boundary_defense_ref": defense["defense_id"],
            "boundary_breached_on": None,
            "legacy_proxy": True,
        }

    boundary = str(defense["breached_on"])
    before_boundary = [row for row in small_up if row["confirmed_on"] <= boundary]
    direct = False
    if before_boundary:
        source_attack = before_boundary[-1]
        opposite_between = [
            row
            for row in attacks_and_defenses["attacks"]
            if row["scale"] == "SMALL"
            and row["direction"] == "DOWN"
            and source_attack["confirmed_on"] <= row["confirmed_on"] <= boundary
        ]
        lows_between = [
            row
            for row in confirmed_pivots
            if row["scale"] == "SMALL"
            and row["side"] == "LOW"
            and source_attack["confirmed_on"] < row["confirmation_date"] <= boundary
        ]
        direct = not opposite_between and not lows_between

    post_boundary_lows = sorted(
        (
            row
            for row in confirmed_pivots
            if row["scale"] == "SMALL" and row["side"] == "LOW" and boundary < row["confirmation_date"] <= as_of
        ),
        key=lambda row: (row["confirmation_date"], row["source_date"], row["ref"]),
    )
    base = {
        "pivot_definition": PIVOT_DEFINITION,
        "boundary_defense_ref": defense["defense_id"],
        "boundary_breached_on": boundary,
        "legacy_proxy": True,
    }
    if not post_boundary_lows:
        return {**base, "phase": "DIRECT_TO_RIGHT" if direct else "RIGHT_AWAIT_PULLBACK"}
    pullback = post_boundary_lows[0]
    latest_close = float(bars[-1]["close"]) if bars else 0.0
    if _cmp(latest_close, float(pullback["price"])) < 0:
        return {**base, "phase": "RIGHT_FAILED", "pullback_pivot_ref": pullback["ref"]}
    later_up = [row for row in small_up if row["confirmed_on"] > pullback["confirmation_date"]]
    return {
        **base,
        "phase": "RR" if later_up else "RL",
        "pullback_pivot_ref": pullback["ref"],
        "continuation_attack_id": later_up[-1]["attack_id"] if later_up else None,
    }


def derive_candidate_hypotheses(
    *, confirmed_pivots: Sequence[Mapping[str, Any]], completed_macd_cycles: Sequence[Mapping[str, Any]] = ()
) -> list[dict[str, Any]]:
    """Enumerate bounded anchor hypotheses without asserting that any is valid."""
    candidates: list[dict[str, Any]] = []
    for scale in SCALES:
        rows = sorted(
            (row for row in confirmed_pivots if row["scale"] == scale),
            key=lambda row: (row["source_date"], row["confirmation_date"], row["ref"]),
        )
        legs: list[dict[str, Any]] = []
        for left, right in zip(rows, rows[1:]):
            if left["side"] == right["side"]:
                continue
            direction = "UP" if left["side"] == "LOW" else "DOWN"
            amplitude = abs(float(right["price"]) - float(left["price"]))
            legs.append(
                {
                    "hypothesis_type": "PIVOT_LEG",
                    "scale": scale,
                    "direction": direction,
                    "start_ref": left["ref"],
                    "end_ref": right["ref"],
                    "available_on": max(left["confirmation_date"], right["confirmation_date"]),
                    "amplitude": amplitude,
                    "asserted_valid_anchor": False,
                }
            )
        candidates.extend(legs[-4:])
    for raw in completed_macd_cycles:
        row = dict(raw)
        if str(row.get("status")) != "CONFIRMED" or not row.get("end"):
            continue
        sign = str(row["sign"]).upper()
        candidate = {
            "hypothesis_type": "MACD_SKELETON",
            "scale": "UNASSIGNED",
            "direction": "UP" if sign == "POSITIVE" else "DOWN",
            "start_ref": f"BAR:{row['low_date'] if sign == 'POSITIVE' else row['high_date']}",
            "end_ref": f"BAR:{row['high_date'] if sign == 'POSITIVE' else row['low_date']}",
            "available_on": str(row.get("confirmed_on") or row["end"]),
            "amplitude": abs(float(row["high"]) - float(row["low"])),
            "asserted_valid_anchor": False,
            "macd_support_only": True,
        }
        candidates.append(candidate)
    for row in candidates:
        row["pivot_definition"] = PIVOT_DEFINITION
        row["hypothesis_id"] = "H-" + _canonical_sha(row)[:20]
    return sorted(candidates, key=lambda row: (row["available_on"], row["hypothesis_id"]))


def material_action_signature(*, direction: str, stop_ref: str | None, position_role: str) -> dict[str, Any]:
    """Canonical signature used to decide whether two semantic routes are materially identical."""
    value = {
        "pivot_definition": PIVOT_DEFINITION,
        "direction": str(direction),
        "stop_ref": stop_ref,
        "position_role": str(position_role),
    }
    return {**value, "signature_sha256": _canonical_sha(value)}


def _objective_signature(state: Mapping[str, Any]) -> dict[str, Any]:
    value = {
        "pivot_definition": PIVOT_DEFINITION,
        "as_of": state["as_of"],
        "dow": {key: row["dow_state"] for key, row in state["dow"].items()},
        "controls": {key: row["state"] for key, row in state["controls"].items()},
        "scale_relationship": state["scale_relationship"],
        "left_right_phase": state["left_right"]["phase"],
        "active_defense_refs": sorted(
            row["defense_id"] for row in state["defenses"] if row["status"] == "ACTIVE"
        ),
        "candidate_hypothesis_ids": [row["hypothesis_id"] for row in state["candidate_hypotheses"]],
    }
    return {
        **value,
        "trade_permission": "NOT_DECIDED_BY_OBJECTIVE_ENGINE",
        "signature_sha256": _canonical_sha(value),
    }


def derive_objective_state(
    *,
    as_of: str,
    bars: Iterable[Mapping[str, Any]],
    confirmed_pivots: Iterable[Mapping[str, Any]],
    completed_macd_cycles: Iterable[Mapping[str, Any]] = (),
    selection_active: bool = True,
    pivot_definition: str = PIVOT_DEFINITION,
) -> dict[str, Any]:
    """Derive a deterministic, outcome-blind state from facts visible at ``as_of``."""
    as_of = _day(as_of)
    if pivot_definition != PIVOT_DEFINITION:
        raise ValueError(f"Candidate 2 baseline only accepts {PIVOT_DEFINITION}")
    normalized_bars = _normalize_bars(as_of, bars)
    normalized_pivots = _normalize_pivots(as_of, confirmed_pivots)
    cycles: list[dict[str, Any]] = []
    for raw in completed_macd_cycles:
        row = dict(raw)
        for field in ("start", "end", "low_date", "high_date", "confirmed_on"):
            if row.get(field) and _day(row[field]) > as_of:
                raise ValueError(f"future MACD cycle is forbidden: {row[field]} > {as_of}")
        if row.get("confirmed_on") and row.get("end") and _day(row["confirmed_on"]) <= _day(row["end"]):
            raise ValueError("MACD cycle confirmation must be after its final same-sign bar")
        cycles.append(row)
    dow_result = derive_dow_states(normalized_pivots)
    dow = dow_result["scales"]
    replay = derive_attacks_and_defenses(bars=normalized_bars, confirmed_pivots=normalized_pivots)
    left_right = derive_left_right_state(
        as_of=as_of,
        bars=normalized_bars,
        confirmed_pivots=normalized_pivots,
        dow_states=dow,
        attacks_and_defenses=replay,
    )
    hypotheses = derive_candidate_hypotheses(
        confirmed_pivots=normalized_pivots, completed_macd_cycles=cycles
    )
    relationship = derive_scale_relationship(replay["controls"])
    state: dict[str, Any] = {
        "engine_version": ENGINE_VERSION,
        "as_of": as_of,
        "pivot_definition": PIVOT_DEFINITION,
        "course_l1_l2_used": False,
        "selection_active": bool(selection_active),
        "input_quality": {
            "bars": len(normalized_bars),
            "confirmed_pivots": len(normalized_pivots),
            "bar_history_start": normalized_bars[0]["date"] if normalized_bars else None,
            "replay_may_be_left_truncated": bool(
                normalized_pivots
                and (not normalized_bars or normalized_bars[0]["date"] > normalized_pivots[0]["confirmation_date"])
            ),
        },
        "dow": dow,
        "controls": replay["controls"],
        "attacks": replay["attacks"],
        "defenses": replay["defenses"],
        "defense_events": replay["defense_events"],
        "scale_relationship": relationship["relationship"],
        "scale_relationship_derivation": relationship,
        "left_right": left_right,
        "candidate_hypotheses": hypotheses,
        "experimental_interfaces": {"course_l1_l2": course_l1_l2_experimental_interface()},
    }
    state["objective_action_signature"] = _objective_signature(state)
    return state


def derive_from_event_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt a frozen V1 anonymous event packet without exposing identity or outcomes."""
    evidence = list(packet.get("evidence") or [])
    bars = [{"date": row["date"], **dict(row.get("values") or {})} for row in evidence if row.get("kind") == "BAR"]
    pivots = [dict(row.get("values") or {}) for row in evidence if row.get("kind") == "CONFIRMED_PIVOT"]
    cycles = [dict(row.get("values") or {}) for row in evidence if row.get("kind") == "COMPLETED_MACD_CYCLE"]
    state = derive_objective_state(
        as_of=str(packet["as_of"]),
        bars=bars,
        confirmed_pivots=pivots,
        completed_macd_cycles=cycles,
        selection_active=bool((packet.get("selection_asof") or {}).get("selected_before_or_on_day")),
    )
    return {
        "review_id": packet.get("review_id"),
        "anonymous_stock_id": packet.get("anonymous_stock_id"),
        **state,
    }


def course_l1_l2_experimental_interface() -> dict[str, Any]:
    """Advertise, but never activate, the future course-faithful pivot stream."""
    return {
        "pivot_definition": EXPERIMENTAL_COURSE_PIVOT_DEFINITION,
        "status": "DISABLED_NOT_IMPLEMENTED",
        "allowed_in_official_result": False,
        "mixed_with_legacy": False,
    }


__all__ = [
    "PIVOT_DEFINITION",
    "EXPERIMENTAL_COURSE_PIVOT_DEFINITION",
    "course_l1_l2_experimental_interface",
    "derive_attacks_and_defenses",
    "derive_candidate_hypotheses",
    "derive_dow_states",
    "derive_from_event_packet",
    "derive_left_right_state",
    "derive_objective_state",
    "derive_scale_relationship",
    "material_action_signature",
]
