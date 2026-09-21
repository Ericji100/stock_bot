from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from trade_monitor.pivot_replay import (
    Candle,
    detect_local_pivots,
    dow_state,
    pair_pivots,
    secondary_pivots,
)

from .anchor_lifecycle import anchor_control, anchor_records, build_anchor_lifecycle


class DeterministicReplayError(ValueError):
    pass


MATERIAL_TIMELINE_EVENTS = {
    "DEFENSE_BROKEN",
    "WORKING_ANCHOR_ESTABLISHED",
    "WORKING_ANCHOR_CHANGED",
    "BACKGROUND_QUADRANT_BASELINE_CHANGED",
    "WORKING_QUADRANT_BASELINE_CHANGED",
    "GRADE_UPGRADE",
    "GRADE_DOWNGRADE",
    "STRUCTURE_INVALIDATED",
    "FALSE_BREAK_RECLAIM",
    "BACKGROUND_ANCHOR_ESTABLISHED",
    "BACKGROUND_ANCHOR_REPLACED",
    "BACKGROUND_DEFENSE_BROKEN",
    "BACKGROUND_DEFENSE_RECLAIMED",
    "REVERSE_CANDIDATE_ESTABLISHED",
    "CHILD_ANCHOR_ESTABLISHED",
    "PROGRAM_SETUP_ARMED",
    "PROGRAM_SETUP_CHANGED",
    "PROGRAM_DEFENSE_AVAILABLE",
    "PROGRAM_QUADRANT_CHANGED",
    "PROGRAM_TAIJI_CHANGED",
    "PROGRAM_METHOD_CHANGED",
    "PROGRAM_NUMBERED_MARKET_CHANGED",
}

# This value is part of the persistent causal-timeline cache identity.  Bump it
# whenever a program-owned state transition changes; otherwise a replay can use
# a timeline produced by older deterministic code even though its per-bar state
# is already running the new code.
DETERMINISTIC_TIMELINE_ENGINE_VERSION = "v6-ai-hybrid-q2-slow-q4-aggressive"
DETERMINISTIC_TIMELINE_ENGINE_SHA256 = hashlib.sha256(
    b"\0".join(
        path.read_bytes()
        for path in (
            Path(__file__),
            Path(__file__).with_name("anchor_lifecycle.py"),
            Path(__file__).parents[1] / "trade_monitor" / "pivot_replay.py",
            Path(__file__).parents[1] / "trade_monitor" / "structured_market_data.py",
        )
    )
).hexdigest()


def build_evidence_ledger(
    structured: Mapping[str, Any],
    *,
    bars: Sequence[Mapping[str, Any]],
    expected_as_of: str,
    session_key: str,
    previous_ledger: Mapping[str, Any] | None = None,
    anchor_lifecycle_enabled: bool = False,
    course_chain_enabled: bool = False,
    decision_authority: str = "PROGRAM",
    trade_direction_policy: str = "BOTH",
    trade_setup_policy: str = "ALL",
) -> dict[str, Any]:
    """Build causal, program-owned market evidence for one replay cutoff.

    Earlier contracts exposed observations only.  The course-chain contract
    additionally publishes a deterministic executable plan; the analyzer may
    explain it but does not own entry, stop, management, or exit decisions.
    """

    expected = _aware(expected_as_of, "expected_as_of")
    direction_policy = str(trade_direction_policy or "BOTH").strip().upper()
    if direction_policy not in {"BOTH", "LONG_ONLY"}:
        raise DeterministicReplayError("trade_direction_policy只允許BOTH或LONG_ONLY。")
    setup_policy = str(trade_setup_policy or "ALL").strip().upper()
    if setup_policy not in {"ALL", "LONG_Q2_Q4_ONLY"}:
        raise DeterministicReplayError("trade_setup_policy只允許ALL或LONG_Q2_Q4_ONLY。")
    if setup_policy == "LONG_Q2_Q4_ONLY" and direction_policy != "LONG_ONLY":
        raise DeterministicReplayError("LONG_Q2_Q4_ONLY必須搭配LONG_ONLY。")
    previous_in_segment = (
        previous_ledger
        if _ledger_matches_monitoring_segment(previous_ledger, expected)
        else None
    )
    causal = structured.get("causal_structure_n2")
    causal = causal if isinstance(causal, Mapping) else {}
    latest = structured.get("latest_closed_k")
    if not isinstance(latest, Mapping):
        raise DeterministicReplayError("缺少最新已收盤K。")

    small = _pivot_group(causal.get("recent_confirmed_pivots"), level="SMALL", expected=expected)
    large = _pivot_group(causal.get("recent_large_pivots"), level="LARGE", expected=expected)
    normalized_bars = _session_bars(bars, expected)
    working_small = _working_pivot_group(normalized_bars, expected=expected)
    small_with_working_endpoint = _merge_pivot_evidence(small, working_small)
    legs = _completed_legs(small, level="SMALL") + _completed_legs(large, level="LARGE")
    for level, pivots in (("SMALL", small_with_working_endpoint), ("LARGE", large)):
        forming = _forming_leg(pivots, normalized_bars, level=level, expected=expected)
        if forming is not None:
            legs.append(forming)

    upgrade_events, promoted_legs = _grade_upgrade_evidence(
        small_with_working_endpoint,
        normalized_bars,
        expected=expected,
    )
    legs.extend(promoted_legs)
    dow_small = _dow(causal.get("dow_small"))
    dow_large = _dow(causal.get("dow_large"))
    current_defenses = [
        item
        for item in (
            _defense(small, normalized_bars, level="SMALL", dow=dow_small),
            _defense(large, normalized_bars, level="LARGE", dow=dow_large),
        )
        if item is not None
    ]
    defenses = _merge_defense_history(
        current_defenses,
        previous_ledger=previous_in_segment,
        bars=normalized_bars,
        session_key=session_key,
    )
    lifecycle_events = _grade_lifecycle_events(
        upgrade_events,
        normalized_bars,
        expected=expected,
    )
    false_break_events = _false_break_reclaim_events(
        pivots=small_with_working_endpoint + large,
        defenses=defenses,
        opening_ranges=structured.get("opening_ranges"),
        bars=normalized_bars,
        expected=expected,
    )
    durable_structure_events = sorted(
        upgrade_events + lifecycle_events,
        key=lambda item: (item["first_seen_at"], item["id"]),
    )[-8:]
    structure_events = sorted(
        durable_structure_events + false_break_events,
        key=lambda item: (item["first_seen_at"], item["id"]),
    )
    trade_levels = _deterministic_trade_levels(
        pivots=small_with_working_endpoint + large,
        structure_events=structure_events,
        indicators=structured.get("indicators"),
        latest=latest,
        expected=expected,
    )
    ledger = {
        "version": 3,
        "as_of": expected.isoformat(),
        "session_key": session_key,
        "latest_closed_k": {
            key: latest.get(key) for key in ("time", "open", "high", "low", "close", "volume")
        },
        # Retain a bounded causal OHLC index so a previously active child
        # defense or opening checkpoint remains both auditable and renderable
        # after the current lifecycle snapshot replaces that child role.
        "recent_bar_levels": [
            {key: item.get(key) for key in ("time", "open", "high", "low", "close")}
            for item in normalized_bars[-30:]
        ],
        "indicators": dict(structured.get("indicators") or {}),
        "opening_ranges": dict(structured.get("opening_ranges") or {}),
        "dow": {"small": dow_small, "large": dow_large},
        "pivots": sorted(small + large, key=lambda item: (item["first_seen_at"], item["level"], item["id"])),
        "working_pivots": [
            item for item in working_small if item["id"] not in {pivot["id"] for pivot in small}
        ],
        "legs": sorted(legs, key=lambda item: (item["end_time"], item["level"], item["id"])),
        "defenses": defenses,
        "structure_events": structure_events,
        "quadrant_evidence": _quadrant_evidence(normalized_bars, structured.get("indicators")),
        "trade_levels": trade_levels,
        "control_candidates": _control_candidates(legs, structure_events),
    }
    if anchor_lifecycle_enabled:
        segment = _monitoring_segment(expected)
        lifecycle_bars = _monitoring_segment_bars(bars, expected)
        lifecycle_session_key = f"{session_key}:{segment['name']}"
        anchor_state = build_anchor_lifecycle(
            lifecycle_bars,
            expected_as_of=expected.isoformat(),
            session_key=lifecycle_session_key,
            session_start=segment["start"].isoformat(),
            course_chain_enabled=course_chain_enabled,
        )
        # The course anchor engine is authoritative for the active small/large
        # Dow defenses.  The legacy pivot projection above can omit a defense
        # that was established by an anchor dynasty, which in turn hides a
        # perfectly causal break-and-reclaim event from the execution gate.
        # Merge those defenses before rebuilding structure events and trade
        # levels so the model never has to infer an executable false break from
        # prose alone.
        defenses = _merge_course_lifecycle_defenses(defenses, anchor_state)
        false_break_events = _false_break_reclaim_events(
            pivots=small_with_working_endpoint + large,
            defenses=defenses,
            opening_ranges=structured.get("opening_ranges"),
            bars=normalized_bars,
            expected=expected,
        )
        structure_events = sorted(
            durable_structure_events + false_break_events,
            key=lambda item: (item["first_seen_at"], item["id"]),
        )
        ledger["defenses"] = defenses
        ledger["structure_events"] = structure_events
        ledger["trade_levels"] = _deterministic_trade_levels(
            pivots=small_with_working_endpoint + large,
            structure_events=structure_events,
            indicators=structured.get("indicators"),
            latest=latest,
            expected=expected,
        )
        ledger["control_candidates"] = _control_candidates(legs, structure_events)
        reference_segment = _reference_monitoring_segment(expected)
        reference_bars = _bars_between(
            bars,
            start=reference_segment["start"],
            end=reference_segment["end"],
        )
        reference_state = None
        if len(reference_bars) >= 2:
            reference_state = build_anchor_lifecycle(
                reference_bars,
                expected_as_of=str(reference_bars[-1]["time"]),
                session_key=f"{session_key}:REFERENCE:{reference_segment['name']}",
                session_start=reference_segment["start"].isoformat(),
                course_chain_enabled=course_chain_enabled,
            )
        ledger.update(
            {
                "version": 4,
                "monitoring_session": {
                    "name": segment["name"],
                    "start": segment["start"].isoformat(),
                    "bar_count": len(lifecycle_bars),
                    "prior_session_role": "REFERENCE_ONLY",
                },
                "anchor_lifecycle": anchor_state,
                "anchor_records": anchor_records(anchor_state),
                "anchor_control": anchor_control(anchor_state),
                "anchor_events": list(anchor_state.get("events") or []),
                "reference_anchor_lifecycle": reference_state,
                "reference_anchor_records": (
                    anchor_records(reference_state) if isinstance(reference_state, Mapping) else []
                ),
            }
        )
        replay_trade_levels = dict(ledger.get("trade_levels") or {})
        continuation_candidate = _continuation_arm_candidate(
            anchor_state,
            expected=expected,
            bars=lifecycle_bars,
        )
        false_break_candidate = _false_break_arm_candidate(
            durable_structure_events + false_break_events,
            expected=expected,
            bars=lifecycle_bars,
            include_causal_checkpoint=direction_policy == "LONG_ONLY",
        )
        opening_range_candidate = None
        compression_candidate = None
        replay_trade_levels["q4_continuation_candidate"] = continuation_candidate
        replay_trade_levels["false_break_arm_candidate"] = false_break_candidate
        replay_trade_levels["opening_range_pullback_candidate"] = opening_range_candidate
        replay_trade_levels["compression_breakout_candidate"] = compression_candidate
        course_method_state = None
        q1_momentum_candidate = None
        q2_failed_reverse_candidate = None
        if course_chain_enabled:
            course_method_state = _derive_program_course_methods(
                anchor_state,
                bars=lifecycle_bars,
                opening_ranges=structured.get("opening_ranges"),
                previous_state=(
                    previous_in_segment.get("course_method_state")
                    if isinstance(previous_in_segment, Mapping)
                    else None
                ),
            )
            q1_momentum_candidate = _yizhi_momentum_entry_candidate(
                anchor_state,
                course_method_state=course_method_state,
                bars=lifecycle_bars,
                expected=expected,
            )
            q2_failed_reverse_candidate = _q2_failed_reverse_entry_candidate(
                continuation_candidate,
                anchor_state=anchor_state,
                course_method_state=course_method_state,
                apply_interpretive_prefilter=not (
                    decision_authority == "AI_HYBRID"
                    and setup_policy == "LONG_Q2_Q4_ONLY"
                ),
            )
            opening_range_candidate = _opening_range_pullback_candidate(
                structured.get("opening_ranges"),
                expected=expected,
                bars=lifecycle_bars,
            )
            compression_candidate = _compression_breakout_candidate(
                anchor_state,
                course_method_state=course_method_state,
                expected=expected,
                bars=lifecycle_bars,
            )
            replay_trade_levels["compression_breakout_candidate"] = compression_candidate
            replay_trade_levels["opening_range_pullback_candidate"] = opening_range_candidate
        replay_trade_levels["q1_momentum_candidate"] = q1_momentum_candidate
        replay_trade_levels["q2_failed_reverse_candidate"] = q2_failed_reverse_candidate
        if decision_authority == "AI_HYBRID" and setup_policy == "LONG_Q2_Q4_ONLY":
            q2_slow_candidate = _q2_slow_outer_expansion_failure_candidate(
                structure_events,
                anchor_state=anchor_state,
                expected=expected,
                bars=lifecycle_bars,
            )
            q4_aggressive_candidate = _q4_aggressive_pullback_reversal_candidate(
                anchor_state,
                expected=expected,
                bars=lifecycle_bars,
                structure_events=structure_events,
                structural_legs=legs,
            )
            replay_trade_levels["q2_slow_outer_expansion_failure_candidate"] = (
                q2_slow_candidate
            )
            replay_trade_levels["q4_aggressive_pullback_reversal_candidate"] = (
                q4_aggressive_candidate
            )
            ai_inventory, ai_advisory_audits = _build_ai_hybrid_q2_q4_candidate_inventory(
                (
                    continuation_candidate,
                    false_break_candidate,
                    q2_failed_reverse_candidate,
                    q2_slow_candidate,
                    q4_aggressive_candidate,
                ),
                expected=expected,
                anchor_state=anchor_state,
                course_method_state=(
                    course_method_state
                    if isinstance(course_method_state, Mapping)
                    else {}
                ),
                structure_events=structure_events,
            )
            replay_trade_levels["ai_candidate_inventory"] = ai_inventory
            replay_trade_levels["ai_armable_setup_keys"] = [
                str(item["setup_key"]) for item in ai_inventory
            ]
            # This full audit remains in the authoritative ledger for later
            # program-vs-AI comparison.  The runner deliberately removes it
            # from the model-facing copy so program quadrant/Taiji conclusions
            # cannot bias the independent course reading.
            replay_trade_levels["ai_candidate_interpretive_audits"] = (
                ai_advisory_audits
            )
        selected_entry_candidate = _select_program_entry_candidate(
            continuation_candidate,
            false_break_candidate,
            q1_momentum_candidate,
            q2_failed_reverse_candidate,
            opening_range_candidate,
            compression_candidate,
            anchor_state=anchor_state,
            allowed_directions=(
                {"LONG"} if direction_policy == "LONG_ONLY" else {"LONG", "SHORT"}
            ),
            allowed_sources=(
                {
                    "ANCHOR_LEG_SEQUENCE",
                    "CONFIRMED_PULLBACK_ENDPOINT_N2",
                    "FALSE_BREAK_RECLAIM",
                    "Q2_FAILED_REVERSE_CANDIDATE",
                }
                if setup_policy == "LONG_Q2_Q4_ONLY"
                else None
            ),
        )
        replay_trade_levels["continuation_arm_candidate"] = selected_entry_candidate
        replay_trade_levels["position_protection_candidates"] = _position_protection_candidates(
            anchor_state,
            bars=lifecycle_bars,
        )
        ledger["trade_levels"] = replay_trade_levels
        if course_chain_enabled:
            assert isinstance(course_method_state, Mapping)
            qualified_candidate, quality_audit = _apply_program_course_entry_quality_gate(
                selected_entry_candidate,
                anchor_state=anchor_state,
                course_method_state=course_method_state,
                structure_events=structure_events,
            )
            course_method_state = _finalize_program_method_scan(
                course_method_state,
                anchor_state=anchor_state,
                selected_candidate=selected_entry_candidate,
                qualified_candidate=qualified_candidate,
                quality_audit=quality_audit,
                candidate_inventory={
                    "Q4_CONTINUATION": continuation_candidate,
                    "FALSE_BREAK": false_break_candidate,
                    "Q1_YIZHI": q1_momentum_candidate,
                    "Q2_FAILED_REVERSE": q2_failed_reverse_candidate,
                    "OPENING_RANGE_PULLBACK": opening_range_candidate,
                    "COMPRESSION_BREAKOUT_SSTV": compression_candidate,
                },
            )
            replay_trade_levels["selected_entry_candidate_before_course_gate"] = (
                dict(selected_entry_candidate)
                if isinstance(selected_entry_candidate, Mapping)
                else None
            )
            replay_trade_levels["continuation_arm_candidate"] = qualified_candidate
            replay_trade_levels["course_entry_quality_audit"] = quality_audit
            if isinstance(selected_entry_candidate, Mapping) and qualified_candidate is None:
                replay_trade_levels["course_filtered_candidate"] = dict(selected_entry_candidate)
            else:
                replay_trade_levels.pop("course_filtered_candidate", None)
            ledger["trade_levels"] = replay_trade_levels
            hybrid = decision_authority == "AI_HYBRID"
            if isinstance(qualified_candidate, Mapping) and hybrid:
                qualified_candidate = dict(qualified_candidate)
                qualified_candidate["decision_authority"] = "AI_HYBRID"
                replay_trade_levels["continuation_arm_candidate"] = qualified_candidate
                ledger["trade_levels"] = replay_trade_levels
            program_trade_policy = {
                "version": 2,
                "actionable_setups": (
                    "PROGRAM_CANDIDATES_AI_DECISION" if hybrid else "PROGRAM_ONLY"
                ),
                "decision_authority": "AI_HYBRID" if hybrid else "PROGRAM",
                "interpretive_authority": "AI" if hybrid else "PROGRAM",
                "hard_fact_authority": "PROGRAM",
                "entry_quality_authority": "PROGRAM_COURSE_GATE_V1",
                "trade_direction_policy": direction_policy,
                "trade_setup_policy": setup_policy,
            }
            if hybrid and setup_policy == "LONG_Q2_Q4_ONLY":
                program_trade_policy.update(
                    {
                        "ai_candidate_hard_gate_authority": "PROGRAM_HARD_FACT_GATE_V1",
                        "ai_candidate_interpretive_prefilter": "PROGRAM_ADVISORY_ONLY_V1",
                    }
                )
            ledger["program_trade_policy"] = program_trade_policy
            ledger["course_method_state"] = course_method_state
            context = anchor_state.get("dow_context") or {}
            ledger["legacy_dow_diagnostic"] = dict(ledger["dow"])
            ledger["dow"] = {
                grade: _course_dow_label(context.get(f"{grade}_state"))
                for grade in ("small", "large")
            }
            ledger["dow_authority"] = "COURSE_ANCHOR_LIFECYCLE"
    return ledger


def _derive_program_course_methods(
    anchor_state: Mapping[str, Any],
    *,
    bars: Sequence[Mapping[str, Any]],
    opening_ranges: Any,
    previous_state: Any = None,
) -> dict[str, Any]:
    """Deterministically scan C-class, left/right and X-method state.

    This classifier deliberately uses relative comparisons against the current
    anchor/Taiji rhythm.  It does not introduce an absolute points threshold.
    It identifies method state only; executable setups still come exclusively
    from ``program_trade_policy``.
    """

    normalized = [dict(item) for item in bars if isinstance(item, Mapping)]
    background = anchor_state.get("background_anchor")
    child = anchor_state.get("child_anchor")
    controller = (
        child
        if isinstance(child, Mapping) and child.get("status") in {None, "ACTIVE"}
        else background
        if isinstance(background, Mapping) and background.get("status") in {None, "ACTIVE"}
        else None
    )
    taiji = anchor_state.get("taiji_context")
    previous_yizhi = (
        previous_state.get("yizhi") if isinstance(previous_state, Mapping) else None
    )
    yizhi = _derive_yizhi_state(
        controller,
        taiji=taiji,
        bars=normalized,
        opening_ranges=opening_ranges,
        previous=previous_yizhi,
    )
    left_right = _derive_left_right_state(anchor_state)
    numbered_market = _derive_numbered_market_state(
        anchor_state,
        previous=(
            previous_state.get("numbered_market")
            if isinstance(previous_state, Mapping)
            else None
        ),
        as_of=str(normalized[-1]["time"]) if normalized else None,
    )
    taiji_mode = taiji.get("engine_mode") if isinstance(taiji, Mapping) else "UNDEFINED"
    taiji_state = taiji.get("program_state") if isinstance(taiji, Mapping) else None
    cclass_mode = (
        "YIZHI_MOMENTUM"
        if yizhi["state"] in {
            "CENTRIFUGAL_CONFIRMED", "GOLD_DRAGON", "K_GOLD_DRAGON",
            "MOMENTUM_CONTINUING",
        }
        else "RESETTING"
        if taiji_state in {"CORRECTION_DESTRUCTIVE", "COPY_FAILED"}
        else taiji_mode
        if taiji_mode in {"TAIJI_ORDERED", "RESETTING", "UNDEFINED"}
        else "UNDEFINED"
    )
    result = {
        "version": 2,
        "authority": "PROGRAM_POLICY_V2",
        "cclass_mode": cclass_mode,
        "yizhi": yizhi,
        "left_right": left_right,
        "numbered_market": numbered_market,
    }
    result["scenario_weights"] = _derive_program_scenario_weights(
        anchor_state,
        cclass_mode=cclass_mode,
        yizhi=yizhi,
    )
    return result


def _derive_numbered_market_state(
    anchor_state: Mapping[str, Any],
    *,
    previous: Any = None,
    as_of: str | None,
) -> dict[str, Any]:
    """Track the course 0/1/2+ market from confirmed directional campaigns.

    The first small or large anchor is not enough by itself: it must own an
    active same-direction Dow defense.  Once 0-market exists, only a formal
    Type-2/Type-3 takeover may increase the number.  A small-scale takeover is
    valid only after the prior numbered direction's defense was recorded broken
    and no intact opposite large background remains.  Grade upgrades,
    reclassification and same-direction re-anchoring cannot inflate the count.
    """

    now = _aware(as_of, "numbered_market.as_of") if as_of else None
    prior = previous if isinstance(previous, Mapping) else {}
    status = str(prior.get("status") or "UNDEFINED")
    number = int(prior.get("number") or 0) if status != "UNDEFINED" else None
    direction = (
        str(prior.get("direction"))
        if prior.get("direction") in {"BULL", "BEAR"}
        else None
    )
    first_direction = (
        str(prior.get("first_direction"))
        if prior.get("first_direction") in {"BULL", "BEAR"}
        else direction
    )
    first_confirmed_at = (
        str(prior.get("first_confirmed_at"))
        if prior.get("first_confirmed_at")
        else None
    )
    last_changed_at = (
        str(prior.get("last_changed_at"))
        if prior.get("last_changed_at")
        else first_confirmed_at
    )
    transitions = [
        dict(item)
        for item in prior.get("transitions", [])
        if isinstance(item, Mapping)
    ]
    consumed = {
        str(item)
        for item in prior.get("consumed_takeover_anchor_refs", [])
        if item
    }
    pending_flip = (
        dict(prior.get("pending_flip"))
        if isinstance(prior.get("pending_flip"), Mapping)
        else None
    )
    active_defense_ref = (
        str(prior.get("active_defense_ref"))
        if prior.get("active_defense_ref")
        else None
    )
    active_defense_time = (
        str(prior.get("active_defense_time"))
        if prior.get("active_defense_time")
        else None
    )
    active_defense_price = prior.get("active_defense_price")

    background = anchor_state.get("background_anchor")
    background = background if isinstance(background, Mapping) else {}
    child = anchor_state.get("child_anchor")
    child = child if isinstance(child, Mapping) else {}
    dow = anchor_state.get("dow_context")
    dow = dow if isinstance(dow, Mapping) else {}

    if status == "UNDEFINED":
        controller = _confirmed_numbered_market_controller(
            background=background,
            child=child,
            dow=dow,
        )
        if controller is not None:
            direction = str(controller["direction"])
            first_direction = direction
            number = 0
            status = "NUMBERED"
            # The anchor may have been observed earlier than the Dow defense.
            # Numbered-market confirmation belongs to the first cutoff where
            # both facts coexist; never backdate it to the anchor's own birth.
            confirmed_at = str(as_of or controller.get("first_seen_at") or "")
            first_confirmed_at = confirmed_at or None
            last_changed_at = first_confirmed_at
            transitions.append(
                {
                    "number": 0,
                    "direction": direction,
                    "confirmed_at": first_confirmed_at,
                    "change_type": "FIRST_CONFIRMED_SESSION_DIRECTION",
                    "source_anchor_ref": controller.get("id"),
                    "source_anchor_first_seen_at": controller.get("first_seen_at"),
                }
            )
            initial_defense = controller.get("defense")
            if isinstance(initial_defense, Mapping):
                active_defense_ref = str(initial_defense.get("id") or "") or None
                active_defense_time = _optional_datetime_text(initial_defense.get("time"))
                active_defense_price = initial_defense.get("price")

    # Preserve the exact prior-direction defense break while the opposite
    # impulse is still developing.  Later lifecycle reconstruction may remove
    # that old child anchor from the current snapshot, so this fact must be
    # carried in numbered-market state rather than rediscovered from recency.
    if status == "NUMBERED" and direction in {"BULL", "BEAR"}:
        direction_controller = _numbered_direction_controller(
            background=background,
            child=child,
            direction=direction,
        )
        direction_defense = (
            direction_controller.get("defense")
            if isinstance(direction_controller, Mapping)
            else None
        )
        if isinstance(direction_defense, Mapping):
            active_defense_ref = str(direction_defense.get("id") or "") or active_defense_ref
            active_defense_time = (
                _optional_datetime_text(direction_defense.get("time"))
                or active_defense_time
            )
            active_defense_price = direction_defense.get("price", active_defense_price)
            if direction_defense.get("state") in {"BROKEN", "BROKEN_AND_RECLAIMED"}:
                broken_at = _optional_datetime_text(direction_defense.get("broken_at"))
                if broken_at is not None:
                    pending_flip = {
                        "from_direction": direction,
                        "defense_ref": direction_defense.get("id"),
                        "defense_time": active_defense_time,
                        "defense_price": active_defense_price,
                        "broken_at": broken_at,
                    }

    takeover = str(background.get("takeover_type") or "")
    takeover_ref = str(background.get("id") or "")
    takeover_direction = str(background.get("direction") or "")
    takeover_at = _optional_datetime_text(background.get("first_seen_at"))
    takeover_visible = (
        takeover_at is not None
        and (now is None or _aware(takeover_at, "takeover.first_seen_at") <= now)
    )
    if (
        status == "NUMBERED"
        and direction in {"BULL", "BEAR"}
        and takeover in {"TYPE2", "TYPE3"}
        and takeover_ref
        and takeover_ref not in consumed
        and takeover_direction in {"BULL", "BEAR"}
        and takeover_direction != direction
        and takeover_visible
    ):
        number = int(number or 0) + 1
        direction = takeover_direction
        last_changed_at = takeover_at
        consumed.add(takeover_ref)
        transitions.append(
            {
                "number": number,
                "direction": direction,
                "confirmed_at": takeover_at,
                "change_type": takeover,
                "source_anchor_ref": takeover_ref,
                "replaced_anchor_ref": background.get("replaced_anchor_ref"),
            }
        )
        pending_flip = None

    retained_large = (
        background
        if background.get("direction") in {"BULL", "BEAR"}
        and background.get("status") in {None, "ACTIVE", "DEGRADED", "DEGRADED_RECLAIMED"}
        else None
    )
    child_takeover = str(child.get("takeover_type") or "")
    child_ref = str(child.get("id") or "")
    child_direction = str(child.get("direction") or "")
    child_takeover_at = _optional_datetime_text(child.get("first_seen_at"))
    child_takeover_visible = bool(
        child_takeover_at is not None
        and (now is None or _aware(child_takeover_at, "child_takeover.first_seen_at") <= now)
    )
    if (
        status == "NUMBERED"
        and direction in {"BULL", "BEAR"}
        and child_takeover in {"TYPE2", "TYPE3"}
        and child_ref
        and child_ref not in consumed
        and child_direction in {"BULL", "BEAR"}
        and child_direction != direction
        and child_takeover_visible
        and (
            retained_large is None
            or retained_large.get("direction") == child_direction
        )
    ):
        number = int(number or 0) + 1
        direction = child_direction
        last_changed_at = child_takeover_at
        consumed.add(child_ref)
        transitions.append(
            {
                "number": number,
                "direction": direction,
                "confirmed_at": child_takeover_at,
                "change_type": f"{child_takeover}_SMALL_TAKEOVER",
                "source_anchor_ref": child_ref,
                "replaced_anchor_ref": child.get("replaced_anchor_ref"),
            }
        )
        new_defense = child.get("defense")
        active_defense_ref = (
            str(new_defense.get("id") or "")
            if isinstance(new_defense, Mapping)
            else None
        ) or None
        active_defense_time = (
            _optional_datetime_text(new_defense.get("time"))
            if isinstance(new_defense, Mapping)
            else None
        )
        active_defense_price = (
            new_defense.get("price") if isinstance(new_defense, Mapping) else None
        )
        pending_flip = None

    # Before a large background exists, the session may establish 0-market on
    # the small grade.  Its later formal opposite anchor is a valid Type-2 only
    # after the recorded old defense break.  Once an intact large background
    # exists, an opposite small child is merely Type-1 and cannot add a number.
    opposite_controller = _confirmed_numbered_market_controller(
        background=background,
        child=child,
        dow=dow,
    )
    small_takeover_allowed = bool(
        status == "NUMBERED"
        and direction in {"BULL", "BEAR"}
        and isinstance(pending_flip, Mapping)
        and pending_flip.get("from_direction") == direction
        and isinstance(opposite_controller, Mapping)
        and opposite_controller.get("level") == "SMALL"
        and opposite_controller.get("direction") in {"BULL", "BEAR"}
        and opposite_controller.get("direction") != direction
        and (
            retained_large is None
            or retained_large.get("direction") == opposite_controller.get("direction")
        )
    )
    if small_takeover_allowed:
        opposite_direction = str(opposite_controller["direction"])
        number = int(number or 0) + 1
        direction = opposite_direction
        confirmed_at = str(as_of or opposite_controller.get("first_seen_at") or "")
        last_changed_at = confirmed_at or last_changed_at
        transitions.append(
            {
                "number": number,
                "direction": direction,
                "confirmed_at": confirmed_at,
                "change_type": "TYPE2_SMALL_DEFENSE_BREAK",
                "source_anchor_ref": opposite_controller.get("id"),
                "broken_defense_ref": pending_flip.get("defense_ref"),
                "broken_at": pending_flip.get("broken_at"),
            }
        )
        new_defense = opposite_controller.get("defense")
        active_defense_ref = (
            str(new_defense.get("id") or "")
            if isinstance(new_defense, Mapping)
            else None
        ) or None
        active_defense_time = (
            _optional_datetime_text(new_defense.get("time"))
            if isinstance(new_defense, Mapping)
            else None
        )
        active_defense_price = (
            new_defense.get("price") if isinstance(new_defense, Mapping) else None
        )
        pending_flip = None

    # A Type-2 anchor already aligned with the numbered direction is a grade
    # promotion, not another flip.  Consume it so it can never be reconsidered
    # after a later state change.
    if (
        takeover in {"TYPE2", "TYPE3"}
        and takeover_ref
        and takeover_visible
        and takeover_direction == direction
    ):
        consumed.add(takeover_ref)

    label = (
        "UNDEFINED"
        if status == "UNDEFINED" or number is None or direction is None
        else "2_PLUS"
        if number >= 2
        else str(number)
    )
    return {
        "version": 1,
        "authority": "PROGRAM_NUMBERED_MARKET_V1",
        "status": status,
        "number": number,
        "label": label,
        "direction": direction,
        "first_direction": first_direction,
        "first_confirmed_at": first_confirmed_at,
        "last_changed_at": last_changed_at,
        "source_anchor_ref": (
            transitions[-1].get("source_anchor_ref") if transitions else None
        ),
        "restriction": (
            "WAIT_FOR_FIRST_DIRECTION"
            if status == "UNDEFINED"
            else "HIGH_NOISE_AI_REVIEW"
            if number is not None and number >= 2
            else "FOLLOW_CURRENT_DIRECTION"
        ),
        "active_defense_ref": active_defense_ref,
        "active_defense_time": active_defense_time,
        "active_defense_price": active_defense_price,
        "pending_flip": pending_flip,
        "transitions": transitions[-8:],
        "consumed_takeover_anchor_refs": sorted(consumed),
    }


def _confirmed_numbered_market_controller(
    *,
    background: Mapping[str, Any],
    child: Mapping[str, Any],
    dow: Mapping[str, Any],
) -> dict[str, Any] | None:
    for anchor, grade in ((background, "large"), (child, "small")):
        if not anchor or anchor.get("direction") not in {"BULL", "BEAR"}:
            continue
        if anchor.get("status") not in {None, "ACTIVE", "DEGRADED_RECLAIMED"}:
            continue
        direction = str(anchor["direction"])
        defense = anchor.get("defense")
        if not (
            isinstance(defense, Mapping)
            and defense.get("state") in {None, "ACTIVE", "BROKEN_AND_RECLAIMED"}
        ):
            continue
        dow_state = str(dow.get(f"{grade}_state") or "UNDEFINED")
        if not dow_state.startswith(direction):
            continue
        return dict(anchor)
    return None


def _numbered_direction_controller(
    *,
    background: Mapping[str, Any],
    child: Mapping[str, Any],
    direction: str,
) -> dict[str, Any] | None:
    retained = {None, "ACTIVE", "DEGRADED", "DEGRADED_RECLAIMED"}
    for anchor in (background, child):
        if (
            anchor
            and anchor.get("direction") == direction
            and anchor.get("status") in retained
        ):
            return dict(anchor)
    return None


def _optional_datetime_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return _aware(value, "datetime").isoformat()


def _derive_program_scenario_weights(
    anchor_state: Mapping[str, Any],
    *,
    cclass_mode: str,
    yizhi: Mapping[str, Any],
) -> dict[str, Any]:
    """Produce stable evidence weights; these are not calibrated probabilities."""

    scores = {"BULL": 1, "RANGE": 2, "BEAR": 1}
    reasons: list[str] = ["NEUTRAL_PRIOR_1_2_1"]
    background = anchor_state.get("background_anchor")
    child = anchor_state.get("child_anchor")
    # Scenario direction is strategic: the highest active background controls
    # the main bias, while a child anchor supplies timing and can add conflict
    # evidence.  Letting the child replace an intact background made the same
    # state say "large bearish" but assign the bull side the controller bonus.
    controller = (
        background
        if isinstance(background, Mapping) and background.get("status") in {None, "ACTIVE"}
        else child
        if isinstance(child, Mapping) and child.get("status") in {None, "ACTIVE"}
        else None
    )
    direction = controller.get("direction") if isinstance(controller, Mapping) else None
    if direction in {"BULL", "BEAR"}:
        scores[str(direction)] += 3
        reasons.append(f"ACTIVE_CONTROLLER_{direction}_PLUS3")
    if (
        isinstance(child, Mapping)
        and child.get("status") in {None, "ACTIVE"}
        and child.get("direction") in {"BULL", "BEAR"}
        and direction in {"BULL", "BEAR"}
        and child.get("direction") != direction
    ):
        scores[str(child["direction"])] += 1
        scores["RANGE"] += 1
        reasons.append(
            f"OPPOSITE_CHILD_{child['direction']}_TIMING_PLUS1_RANGE_PLUS1"
        )

    dow = anchor_state.get("dow_context")
    if isinstance(dow, Mapping):
        for grade, points in (("large", 3), ("small", 2)):
            state = dow.get(f"{grade}_state")
            if state in {"BULL", "BEAR"}:
                scores[str(state)] += points
                reasons.append(f"{grade.upper()}_DOW_{state}_PLUS{points}")

    quadrant = anchor_state.get("quadrant_context")
    working_quadrant = quadrant.get("working_primary") if isinstance(quadrant, Mapping) else None
    if working_quadrant in {"Q1", "Q4"} and direction in {"BULL", "BEAR"}:
        scores[str(direction)] += 2
        reasons.append(f"{working_quadrant}_{direction}_PLUS2")
    elif working_quadrant == "Q2":
        scores["RANGE"] += 2
        if direction in {"BULL", "BEAR"}:
            opposite = "BEAR" if direction == "BULL" else "BULL"
            scores[opposite] += 1
        reasons.append("Q2_RANGE_PLUS2_OPPOSITE_PLUS1")
    elif working_quadrant == "Q3":
        scores["RANGE"] += 3
        reasons.append("Q3_RANGE_PLUS3")
    elif working_quadrant in {"TRANSITION", "UNDEFINED", None} and direction in {"BULL", "BEAR"}:
        scores["RANGE"] += 2
        reasons.append("QUADRANT_UNRESOLVED_RANGE_PLUS2")

    taiji = anchor_state.get("taiji_context")
    taiji_state = taiji.get("program_state") if isinstance(taiji, Mapping) else None
    if direction in {"BULL", "BEAR"} and taiji_state in {"COPY_SUCCESS", "COPY_FORMING"}:
        points = 2 if taiji_state == "COPY_SUCCESS" else 1
        scores[str(direction)] += points
        reasons.append(f"TAIJI_{taiji_state}_{direction}_PLUS{points}")
    elif direction in {"BULL", "BEAR"} and taiji_state in {"CORRECTION_HELD", "CORRECTION_FORMING"}:
        scores[str(direction)] += 1
        reasons.append(f"TAIJI_{taiji_state}_{direction}_PLUS1")
    elif taiji_state in {"COPY_WEAKENED", "COPY_FAILED", "CORRECTION_DESTRUCTIVE"}:
        scores["RANGE"] += 2
        reasons.append(f"TAIJI_{taiji_state}_RANGE_PLUS2")

    reverse = anchor_state.get("reverse_candidate")
    if isinstance(reverse, Mapping) and reverse.get("direction") in {"BULL", "BEAR"}:
        scores[str(reverse["direction"])] += 1
        reasons.append(f"REVERSE_CANDIDATE_{reverse['direction']}_PLUS1")

    if cclass_mode == "YIZHI_MOMENTUM" and yizhi.get("direction") in {"BULL", "BEAR"}:
        scores[str(yizhi["direction"])] += 3
        reasons.append(f"YIZHI_{yizhi['direction']}_PLUS3")

    total = sum(scores.values())
    exact = {key: scores[key] * 100 / total for key in scores}
    weights = {key: int(exact[key]) for key in scores}
    remainder = 100 - sum(weights.values())
    order = sorted(scores, key=lambda key: (exact[key] - weights[key], scores[key], key), reverse=True)
    for key in order[:remainder]:
        weights[key] += 1
    return {
        "bull": weights["BULL"],
        "range": weights["RANGE"],
        "bear": weights["BEAR"],
        "raw_scores": scores,
        "reason_codes": reasons,
        "calibration": "RELATIVE_EVIDENCE_WEIGHT_NOT_EMPIRICAL_PROBABILITY",
        "authority": "PROGRAM_POLICY_V1",
    }


def _finalize_program_method_scan(
    methods: Mapping[str, Any],
    *,
    anchor_state: Mapping[str, Any],
    selected_candidate: Mapping[str, Any] | None,
    qualified_candidate: Mapping[str, Any] | None,
    quality_audit: Mapping[str, Any],
    candidate_inventory: Mapping[str, Mapping[str, Any] | None],
) -> dict[str, Any]:
    """Publish a complete, model-independent method/X-process audit."""

    result = dict(methods)
    numbered = result.get("numbered_market")
    numbered = numbered if isinstance(numbered, Mapping) else {}
    quadrant = anchor_state.get("quadrant_context")
    quadrant = quadrant if isinstance(quadrant, Mapping) else {}
    taiji = anchor_state.get("taiji_context")
    taiji = taiji if isinstance(taiji, Mapping) else {}
    dow = anchor_state.get("dow_context")
    dow = dow if isinstance(dow, Mapping) else {}
    yizhi = result.get("yizhi")
    yizhi = yizhi if isinstance(yizhi, Mapping) else {}
    left_right = result.get("left_right")
    left_right = left_right if isinstance(left_right, Mapping) else {}

    if isinstance(qualified_candidate, Mapping):
        x_stage = "ENTRY_EXECUTION"
        x_reason = "PROGRAM_EXECUTABLE_CANDIDATE"
    elif isinstance(selected_candidate, Mapping):
        x_stage = "OPPORTUNITY_GRADING"
        x_reason = "PROGRAM_CANDIDATE_REJECTED_OR_OBSERVATION_ONLY"
    elif numbered.get("status") == "NUMBERED":
        x_stage = "ANCHOR_LENS_SELECTION"
        x_reason = "DIRECTION_FIXED_WAITING_FOR_EXECUTABLE_PATTERN"
    elif anchor_state.get("working_leg") or anchor_state.get("child_anchor"):
        x_stage = "FIRST_ENDPOINT"
        x_reason = "SESSION_DIRECTION_NOT_YET_CONFIRMED"
    else:
        x_stage = "OPENING_EVIDENCE"
        x_reason = "WAITING_FOR_FIRST_CAUSAL_ENDPOINT"

    selected_source = (
        str(selected_candidate.get("candidate_source") or "")
        if isinstance(selected_candidate, Mapping)
        else ""
    )
    qualified_source = (
        str(qualified_candidate.get("candidate_source") or "")
        if isinstance(qualified_candidate, Mapping)
        else ""
    )
    setup_family_by_source = {
        "ANCHOR_LEG_SEQUENCE": "Q4_CONTINUATION",
        "CONFIRMED_PULLBACK_ENDPOINT_N2": "Q4_CONTINUATION",
        "FALSE_BREAK_RECLAIM": "FALSE_BREAK",
        "YIZHI_EARLY_BREAKOUT": "Q1_YIZHI",
        "Q2_FAILED_REVERSE_CANDIDATE": "Q2_FAILED_REVERSE",
        "OPENING_RANGE_BREAKOUT_RETEST": "OPENING_RANGE_PULLBACK",
        "COMPRESSION_BREAKOUT_SSTV": "COMPRESSION_BREAKOUT_SSTV",
    }
    scan: dict[str, Any] = {
        "NUMBERED_MARKET": {
            "status": "CONFIRMED" if numbered.get("status") == "NUMBERED" else "FORMING",
            "state": numbered.get("label"),
            "direction": numbered.get("direction"),
        },
        "QUADRANT": {
            "status": (
                "CLASSIFIED"
                if quadrant.get("working_primary") in {"Q1", "Q2", "Q3", "Q4"}
                else "FORMING"
            ),
            "state": quadrant.get("working_primary"),
        },
        "TAIJI": {
            "status": str(taiji.get("program_state") or "UNDEFINED"),
            "eligible": result.get("cclass_mode") == "TAIJI_ORDERED",
        },
        "YIZHI": {
            "status": str(yizhi.get("state") or "NONE"),
            "direction": yizhi.get("direction"),
            "eligible": result.get("cclass_mode") == "YIZHI_MOMENTUM",
        },
        "LEFT_RIGHT": {
            "status": str(left_right.get("state") or "NONE"),
            "direction": left_right.get("direction"),
        },
        "DOW": {
            "status": "CLASSIFIED" if dow.get("small_state") != "UNDEFINED" else "FORMING",
            "small": dow.get("small_state"),
            "large": dow.get("large_state"),
        },
    }
    for family, candidate in candidate_inventory.items():
        scan[family] = {
            "status": (
                "EXECUTABLE"
                if qualified_source and setup_family_by_source.get(qualified_source) == family
                else "OBSERVATION_ONLY"
                if selected_source and setup_family_by_source.get(selected_source) == family
                else "FORMING"
                if isinstance(candidate, Mapping)
                else "NOT_PRESENT"
            ),
            "setup_key": candidate.get("setup_key") if isinstance(candidate, Mapping) else None,
        }
    scan["X_PROCESS"] = {"status": x_stage, "reason_code": x_reason}

    result.update(
        {
            "x_stage": x_stage,
            "x_reason_code": x_reason,
            "x_quality_gate_status": quality_audit.get("status"),
            "method_scan": scan,
        }
    )
    return result


def _derive_yizhi_state(
    controller: Mapping[str, Any] | None,
    *,
    taiji: Any,
    bars: list[dict[str, Any]],
    opening_ranges: Any,
    previous: Any = None,
) -> dict[str, Any]:
    if not isinstance(controller, Mapping) or len(bars) < 2:
        return {
            "state": "NONE",
            "direction": None,
            "quality": "INSUFFICIENT",
            "reason_codes": ["NO_ACTIVE_ANCHOR_OR_TWO_BAR_SEQUENCE"],
        }

    last_two = bars[-2:]
    directions = [
        "BULL" if float(item["close"]) > float(item["open"])
        else "BEAR" if float(item["close"]) < float(item["open"])
        else "FLAT"
        for item in last_two
    ]
    direction = directions[0] if directions[0] == directions[1] and directions[0] != "FLAT" else None
    if direction is None:
        fresh = {
            "state": "NONE",
            "direction": None,
            "quality": "NO_DIRECTIONAL_PAIR",
            "reason_codes": ["LATEST_TWO_BARS_NOT_DIRECTIONALLY_ALIGNED"],
        }
        return _stabilize_yizhi_state(fresh, previous=previous, controller=controller, bars=bars)

    two_ranges = [float(item["high"]) - float(item["low"]) for item in last_two]
    two_bodies = [abs(float(item["close"]) - float(item["open"])) for item in last_two]
    prior_two = bars[-4:-2]
    range_expanded = len(prior_two) == 2 and sum(two_ranges) > sum(
        float(item["high"]) - float(item["low"]) for item in prior_two
    )
    prior_body_context = bars[-8:-2]
    prior_bodies = sorted(
        abs(float(item["close"]) - float(item["open"]))
        for item in prior_body_context
    )
    if prior_bodies:
        middle = len(prior_bodies) // 2
        prior_body_median = (
            prior_bodies[middle]
            if len(prior_bodies) % 2
            else (prior_bodies[middle - 1] + prior_bodies[middle]) / 2.0
        )
    else:
        prior_body_median = None
    medium_long_bodies = bool(prior_bodies) and all(
        body >= float(prior_body_median)
        and full_range > 0
        and body / full_range >= 0.5
        for body, full_range in zip(two_bodies, two_ranges)
    )
    two_slope = abs(float(last_two[-1]["close"]) - float(last_two[0]["open"])) / len(last_two)
    parent_slope = None
    if isinstance(taiji, Mapping):
        evidence = taiji.get("leg_evidence")
        parent = evidence.get("current_parent") if isinstance(evidence, Mapping) else None
        if isinstance(parent, Mapping) and isinstance(parent.get("slope_points_per_minute"), (int, float)):
            parent_slope = float(parent["slope_points_per_minute"])
    prior_slope = None
    if len(prior_two) == 2:
        prior_slope = abs(
            float(prior_two[-1]["close"]) - float(prior_two[0]["open"])
        ) / len(prior_two)
    slope_baseline = parent_slope if parent_slope is not None else prior_slope
    slope_baseline_source = "TAIJI_PARENT" if parent_slope is not None else "PREVIOUS_TWO_BARS"
    slope_accelerated = slope_baseline is not None and two_slope > slope_baseline

    latest = last_two[-1]
    # ``latest_extreme_price`` already includes the current candle's wick.  A
    # close can therefore never break it on the same candle (bull close <= high,
    # bear close >= low).  Compare against the extreme that was known at the
    # preceding cutoff when the same controller was already active.  On the
    # first cutoff of a newly established controller, its frozen first extreme
    # is the only causal continuation boundary available.
    same_previous_controller = bool(
        isinstance(previous, Mapping)
        and previous.get("controller_ref") == controller.get("id")
    )
    previous_controller_extreme = (
        previous.get("controller_extreme_price")
        if same_previous_controller and isinstance(previous, Mapping)
        else None
    )
    controller_extreme = (
        previous_controller_extreme
        if isinstance(previous_controller_extreme, (int, float))
        and not isinstance(previous_controller_extreme, bool)
        else controller.get("first_extreme_price") or controller.get("latest_extreme_price")
    )
    breakout_reference = (
        "PREVIOUS_CUTOFF_CONTROLLER_EXTREME"
        if isinstance(previous_controller_extreme, (int, float))
        and not isinstance(previous_controller_extreme, bool)
        else "FROZEN_FIRST_CONTROLLER_EXTREME"
    )
    anchor_breakout = False
    if isinstance(controller_extreme, (int, float)) and not isinstance(controller_extreme, bool):
        anchor_breakout = (
            float(latest["close"]) > float(controller_extreme)
            if direction == "BULL"
            else float(latest["close"]) < float(controller_extreme)
        )
    ranges = opening_ranges if isinstance(opening_ranges, Mapping) else {}
    boundary_breakout = False
    for key in ("or5", "or15"):
        boundary = ranges.get(key)
        if not isinstance(boundary, Mapping):
            continue
        level = boundary.get("high") if direction == "BULL" else boundary.get("low")
        if isinstance(level, (int, float)) and not isinstance(level, bool):
            boundary_breakout = boundary_breakout or (
                float(latest["close"]) > float(level)
                if direction == "BULL"
                else float(latest["close"]) < float(level)
            )
    structural_breakout = anchor_breakout or boundary_breakout

    # Ordered highs/lows describe dragon shape, but the course does not allow
    # that shape alone to establish Yizhi.  Confirmation also requires two
    # same-direction medium/long bodies, range expansion, a steeper slope and
    # a causal structural break.  Keeping these as separate evidence prevents
    # a tiny candle plus one breakout candle from being promoted to momentum.
    dragon = _dragon_quality(bars[-4:], direction)
    hard_confirmation = all(
        (medium_long_bodies, range_expanded, slope_accelerated, structural_breakout)
    )
    if dragon in {"GOLD_DRAGON", "K_GOLD_DRAGON"} and hard_confirmation:
        state = dragon
        quality = "CONFIRMED"
    elif hard_confirmation:
        state = "CENTRIFUGAL_CONFIRMED"
        quality = "CONFIRMED"
    elif structural_breakout and sum(
        (medium_long_bodies, range_expanded, slope_accelerated)
    ) >= 2:
        state = "CENTRIFUGAL_CANDIDATE"
        quality = "FORMING"
    else:
        state = "NONE"
        quality = "NOT_ESTABLISHED"
    fresh = {
        "state": state,
        "direction": direction,
        "quality": quality,
        "start_time": last_two[0].get("time"),
        "latest_time": latest.get("time"),
        "last_close": float(latest["close"]),
        "controller_ref": controller.get("id"),
        "controller_extreme_price": controller.get("latest_extreme_price")
        or controller.get("first_extreme_price"),
        "controller_extreme_time": controller.get("latest_extreme_time")
        or controller.get("first_extreme_time"),
        "breakout_reference_price": controller_extreme,
        "breakout_reference": breakout_reference,
        "two_bar_slope": round(two_slope, 4),
        "parent_slope": parent_slope,
        "slope_baseline": round(float(slope_baseline), 4) if slope_baseline is not None else None,
        "slope_baseline_source": slope_baseline_source if slope_baseline is not None else None,
        "prior_body_median": round(float(prior_body_median), 4) if prior_body_median is not None else None,
        "reason_codes": [
            "TWO_DIRECTIONAL_BARS",
            "MEDIUM_LONG_BODIES" if medium_long_bodies else "BODIES_NOT_MEDIUM_LONG",
            "RANGE_EXPANDED" if range_expanded else "RANGE_NOT_EXPANDED",
            "SLOPE_ACCELERATED" if slope_accelerated else "SLOPE_NOT_ACCELERATED",
            "STRUCTURAL_BREAKOUT" if structural_breakout else "NO_STRUCTURAL_BREAKOUT",
        ],
    }
    return _stabilize_yizhi_state(fresh, previous=previous, controller=controller, bars=bars)


def _stabilize_yizhi_state(
    fresh: dict[str, Any],
    *,
    previous: Any,
    controller: Mapping[str, Any],
    bars: list[dict[str, Any]],
) -> dict[str, Any]:
    controller_direction = str(controller.get("direction") or "")
    controller_ref = controller.get("id")
    if fresh.get("direction") in {"BULL", "BEAR"} and fresh.get("direction") != controller_direction:
        fresh["state"] = "COUNTERTREND_ACCELERATION"
        fresh["quality"] = "WATCH_ONLY_UNTIL_REVERSE_ANCHOR"
        fresh["reason_codes"] = [
            *fresh.get("reason_codes", []),
            "OPPOSES_ACTIVE_ANCHOR_NOT_YIZHI_CONTROL",
        ]
        return fresh

    active_states = {
        "CENTRIFUGAL_CONFIRMED", "GOLD_DRAGON", "K_GOLD_DRAGON", "MOMENTUM_CONTINUING"
    }
    if fresh.get("state") in active_states and not (
        isinstance(previous, Mapping)
        and previous.get("state") in active_states
        and previous.get("controller_ref") == controller_ref
        and previous.get("direction") == controller_direction
    ):
        fresh["activation_time"] = fresh.get("latest_time")
    if not isinstance(previous, Mapping) or previous.get("state") not in active_states:
        return fresh
    if previous.get("controller_ref") != controller_ref or previous.get("direction") != controller_direction:
        return fresh
    if fresh.get("state") in active_states:
        return {
            **fresh,
            "activation_time": previous.get("activation_time") or previous.get("latest_time"),
        }
    if not bars:
        return fresh
    latest = bars[-1]
    latest_close = float(latest["close"])
    previous_close = previous.get("last_close")
    directional_bar = (
        float(latest["close"]) > float(latest["open"])
        if controller_direction == "BULL"
        else float(latest["close"]) < float(latest["open"])
    )
    favorable_close = (
        isinstance(previous_close, (int, float))
        and (
            latest_close >= float(previous_close)
            if controller_direction == "BULL"
            else latest_close <= float(previous_close)
        )
    )
    if directional_bar and favorable_close:
        return {
            **fresh,
            "state": "MOMENTUM_CONTINUING",
            "direction": controller_direction,
            "quality": "CONFIRMED_CONTINUING",
            "controller_ref": controller_ref,
            "last_close": latest_close,
            "latest_time": latest.get("time"),
            "activation_time": previous.get("activation_time") or previous.get("latest_time"),
            "reason_codes": ["PRIOR_YIZHI_ACTIVE", "DIRECTIONAL_CLOSE_CONTINUED"],
        }
    return {
        **fresh,
        "state": "MOMENTUM_FAILED",
        "direction": controller_direction,
        "quality": "FAILED_RETURN_TO_TAIJI_OR_RESET",
        "controller_ref": controller_ref,
        "last_close": latest_close,
        "latest_time": latest.get("time"),
        "activation_time": previous.get("activation_time") or previous.get("latest_time"),
        "reason_codes": ["PRIOR_YIZHI_ACTIVE", "DIRECTIONAL_CONTINUITY_FAILED"],
    }


def _yizhi_momentum_entry_candidate(
    anchor_state: Mapping[str, Any],
    *,
    course_method_state: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    expected: datetime,
) -> dict[str, Any] | None:
    """Create the separate Q1/Yizhi early-momentum execution contract.

    Q1 is entered on an early close beyond the preceding acceleration candle
    and uses the signal candle's opposite extreme.  It never borrows the wide
    full-correction stop used by Q4.  The two-minute activation allowance is a
    versioned execution window, not a historical points threshold.
    """

    yizhi = course_method_state.get("yizhi")
    if not isinstance(yizhi, Mapping) or yizhi.get("state") not in {
        "CENTRIFUGAL_CONFIRMED",
        "GOLD_DRAGON",
        "K_GOLD_DRAGON",
        "MOMENTUM_CONTINUING",
    }:
        return None
    direction = str(yizhi.get("direction") or "")
    if direction not in {"BULL", "BEAR"}:
        return None
    activation = yizhi.get("activation_time") or yizhi.get("latest_time")
    if not isinstance(activation, str):
        return None
    activated_at = _aware(activation, "yizhi.activation_time")
    if expected < activated_at or expected - activated_at > timedelta(minutes=2):
        return None

    quadrant = anchor_state.get("quadrant_context")
    quadrant = quadrant if isinstance(quadrant, Mapping) else {}
    primary = str(quadrant.get("working_primary") or "UNDEFINED")
    candidates = {
        str(item) for item in quadrant.get("working_candidates", [])
        if item in {"Q1", "Q2", "Q3", "Q4"}
    }
    trend = str(quadrant.get("working_trend_dynamics") or "UNCLEAR")
    if not (
        primary == "Q1"
        or (primary == "TRANSITION" and "Q1" in candidates and trend == "INCREASING")
    ):
        return None

    controller = _retained_course_controller(anchor_state)
    if not isinstance(controller, Mapping):
        return None
    if not (
        controller.get("direction") == direction
        or _active_opposite_child_confirms_direction(anchor_state, direction)
    ):
        return None

    normalized = sorted(
        [dict(item) for item in bars if isinstance(item, Mapping)],
        key=lambda item: str(item.get("time")),
    )
    if len(normalized) < 2:
        return None
    previous, signal = normalized[-2:]
    previous_at = _aware(previous.get("time"), "yizhi.previous.time")
    signal_at = _aware(signal.get("time"), "yizhi.signal.time")
    if signal_at != expected or signal_at - previous_at != timedelta(minutes=1):
        return None
    if direction == "BULL":
        trigger = _positive_number(previous.get("high"), "yizhi.previous.high")
        crossed = _positive_number(signal.get("close"), "yizhi.signal.close") > trigger
        stop_source = _positive_number(signal.get("low"), "yizhi.signal.low")
        side = "LONG"
        operator = "CLOSE_ABOVE"
    else:
        trigger = _positive_number(previous.get("low"), "yizhi.previous.low")
        crossed = _positive_number(signal.get("close"), "yizhi.signal.close") < trigger
        stop_source = _positive_number(signal.get("high"), "yizhi.signal.high")
        side = "SHORT"
        operator = "CLOSE_BELOW"
    if not crossed:
        return None
    atr = _atr_from_bar_records(normalized, 14) if len(normalized) >= 15 else None
    buffer = round(max(1.0, float(atr) * 0.2), 1) if atr is not None else 1.0
    stop = stop_source - buffer if side == "LONG" else stop_source + buffer
    return {
        "setup_key": _id("SETUP", "YIZHI_EARLY_MOMENTUM", side, activation),
        "setup_name": "多方一之早期動能" if side == "LONG" else "空方一之早期動能",
        "direction": side,
        "stage": "ARMED",
        "trigger_operator": operator,
        "trigger_level": trigger,
        "trigger_time": previous_at.isoformat(),
        "stop_source_time": signal_at.isoformat(),
        "stop_source_price": stop_source,
        "first_seen_at": signal_at.isoformat(),
        "valid_bars": 2,
        "authority": "PROGRAM_CANDIDATE_NOT_AUTOMATIC_ENTRY",
        "candidate_source": "YIZHI_EARLY_BREAKOUT",
        "entry_strategy": "Q1_YIZHI_EARLY_MOMENTUM",
        "stop_buffer_points": buffer,
        "stop_price": round(stop, 1),
        "stop_policy": "SIGNAL_CANDLE_OPPOSITE_EXTREME_PLUS_0_2_ATR_ENGINEERING_BUFFER",
        "decision_authority": "PROGRAM",
        "behavior_policy": "Q1_YIZHI_FAST_CONTINUATION",
        "behavior_max_wait_bars": 3,
        "behavior_trigger_level": trigger,
        "behavior_obstacles": [
            {
                "price": trigger,
                "role": "TRIGGER",
                "source_time": previous_at.isoformat(),
                "source_field": "high" if side == "LONG" else "low",
            }
        ],
    }


def _q2_failed_reverse_entry_candidate(
    continuation: Mapping[str, Any] | None,
    *,
    anchor_state: Mapping[str, Any],
    course_method_state: Mapping[str, Any],
    apply_interpretive_prefilter: bool = True,
) -> dict[str, Any] | None:
    """Convert a held counter-move endpoint into a parent-aligned Q2 plan.

    Q2 is not a relaxed Q4 gate.  This contract applies only when an opposite
    reverse candidate has matured but has not taken over, the retained course
    controller and its formal defense are still active, and the counter-move
    ends at the same price as the confirmed execution endpoint.  It therefore
    represents failure of the attempted reversal, not every mechanical n=2
    turn that happens while the quadrant is noisy.
    """

    if not isinstance(continuation, Mapping) or continuation.get("candidate_source") not in {
        "ANCHOR_LEG_SEQUENCE",
        "CONFIRMED_PULLBACK_ENDPOINT_N2",
    }:
        return None
    candidate_direction = _candidate_course_direction(continuation)
    controller = _retained_course_controller(anchor_state)
    if (
        candidate_direction not in {"BULL", "BEAR"}
        or not isinstance(controller, Mapping)
        or controller.get("direction") != candidate_direction
    ):
        return None
    defense = controller.get("defense")
    if not (
        isinstance(defense, Mapping)
        and defense.get("id")
        and defense.get("state") in {None, "ACTIVE"}
    ):
        return None

    reverse = anchor_state.get("reverse_candidate")
    opposite = "BEAR" if candidate_direction == "BULL" else "BULL"
    if not (
        isinstance(reverse, Mapping)
        and reverse.get("direction") == opposite
        and reverse.get("status") in {"QUALIFIED", "AWAITING_CONTINUATION"}
    ):
        return None
    working = anchor_state.get("working_leg")
    if not isinstance(working, Mapping) or working.get("direction") != opposite:
        return None

    if apply_interpretive_prefilter:
        quadrant = anchor_state.get("quadrant_context")
        quadrant = quadrant if isinstance(quadrant, Mapping) else {}
        primary = str(quadrant.get("working_primary") or "UNDEFINED")
        candidates = {
            str(item)
            for item in quadrant.get("working_candidates", [])
            if item in {"Q1", "Q2", "Q3", "Q4"}
        }
        if primary != "Q2" and not (primary == "TRANSITION" and "Q2" in candidates):
            return None
        taiji = anchor_state.get("taiji_context")
        taiji = taiji if isinstance(taiji, Mapping) else {}
        if not (
            str(course_method_state.get("cclass_mode") or "") == "RESETTING"
            and taiji.get("program_state") in {"CORRECTION_DESTRUCTIVE", "COPY_FAILED"}
        ):
            return None

    endpoint_price = continuation.get("stop_source_price")
    reverse_price = reverse.get("current_extreme_price")
    if not (
        isinstance(endpoint_price, (int, float))
        and not isinstance(endpoint_price, bool)
        and isinstance(reverse_price, (int, float))
        and not isinstance(reverse_price, bool)
        and abs(float(endpoint_price) - float(reverse_price)) <= 1e-9
    ):
        return None

    selected = dict(continuation)
    selected.update(
        {
            "setup_key": _id(
                "SETUP",
                "Q2_FAILED_REVERSE",
                str(controller.get("id") or "CONTROLLER"),
                str(reverse.get("id") or "REVERSE"),
                str(continuation.get("stop_source_time") or "ENDPOINT"),
            ),
            "setup_name": (
                "多方Q2反向失敗回轉"
                if candidate_direction == "BULL"
                else "空方Q2反向失敗回轉"
            ),
            "candidate_source": "Q2_FAILED_REVERSE_CANDIDATE",
            "entry_strategy": "Q2_FAILED_COUNTERTREND_REVERSAL",
            "behavior_policy": "Q2_FAST_REVERSAL_CONFIRMATION",
            "behavior_max_wait_bars": 3,
            "q2_reverse_candidate_ref": reverse.get("id"),
            "q2_controller_ref": controller.get("id"),
            "q2_controller_defense_ref": defense.get("id"),
            "decision_authority": "PROGRAM",
        }
    )
    return selected


def _causal_candidate_bars(
    bars: Sequence[Mapping[str, Any]] | None,
    *,
    expected: datetime,
) -> list[dict[str, Any]]:
    """Return only closed OHLC facts available at ``expected``."""

    result: list[dict[str, Any]] = []
    for raw in bars or ():
        if not isinstance(raw, Mapping) or not isinstance(raw.get("time"), str):
            continue
        at = _aware(raw["time"], "candidate.bar.time")
        if at > expected:
            continue
        result.append(
            {
                "time": at.isoformat(),
                "open": _positive_number(raw.get("open"), "candidate.bar.open"),
                "high": _positive_number(raw.get("high"), "candidate.bar.high"),
                "low": _positive_number(raw.get("low"), "candidate.bar.low"),
                "close": _positive_number(raw.get("close"), "candidate.bar.close"),
                "volume": raw.get("volume"),
            }
        )
    result.sort(key=lambda item: item["time"])
    return result


def _candidate_fact_hash(*parts: Any) -> str:
    material = "\0".join(str(part) for part in parts).encode("utf-8")
    return "FACTS-" + hashlib.sha256(material).hexdigest()


def _candidate_leg_ref(leg: Mapping[str, Any], *, anchor_ref: str) -> str:
    explicit = leg.get("id")
    if isinstance(explicit, str) and explicit:
        return explicit
    return _id(
        "LEG",
        anchor_ref,
        str(leg.get("direction") or "UNKNOWN"),
        str(leg.get("start_time") or "START"),
        str(leg.get("end_time") or "END"),
    )


def _causal_comparison_legs(
    anchor_state: Mapping[str, Any],
    *,
    anchor_ref: str,
    expected: datetime,
) -> list[dict[str, Any]]:
    quadrant = anchor_state.get("quadrant_context")
    if not isinstance(quadrant, Mapping):
        return []
    selected: Mapping[str, Any] | None = None
    for key in ("child_comparisons", "same_grade_comparisons"):
        raw = quadrant.get(key)
        if not isinstance(raw, Mapping):
            continue
        if raw.get("anchor_ref") == anchor_ref:
            selected = raw
            break
    raw_legs = selected.get("legs") if isinstance(selected, Mapping) else None
    result: list[dict[str, Any]] = []
    for raw in raw_legs if isinstance(raw_legs, list) else []:
        if not isinstance(raw, Mapping):
            continue
        observed = raw.get("observable_at") or raw.get("first_seen_at") or raw.get("end_time")
        if not isinstance(observed, str) or _aware(observed, "candidate.leg.observable_at") > expected:
            continue
        end_time = raw.get("end_time")
        if not isinstance(end_time, str) or _aware(end_time, "candidate.leg.end_time") > expected:
            continue
        result.append(dict(raw))
    result.sort(key=lambda item: (str(item.get("end_time")), str(item.get("start_time"))))
    return result


def _candidate_atr_buffer(records: Sequence[Mapping[str, Any]]) -> tuple[float, str]:
    atr = _atr_from_bar_records([dict(item) for item in records], 14) if len(records) >= 15 else None
    if atr is None:
        return 1.0, "ONE_POINT_PRE_ATR14_ENGINEERING_BUFFER"
    return round(max(1.0, float(atr) * 0.2), 1), "CURRENT_ATR14_ENGINEERING_BUFFER"


def _structure_bar_count(
    records: Sequence[Mapping[str, Any]],
    *,
    start: str,
    end: str,
) -> int:
    left = _aware(start, "candidate.structure.start")
    right = _aware(end, "candidate.structure.end")
    return max(
        1,
        sum(
            1
            for item in records
            if left <= _aware(item.get("time"), "candidate.structure.bar") <= right
        ),
    )


def _q2_slow_outer_expansion_failure_candidate(
    structure_events: Sequence[Mapping[str, Any]],
    *,
    anchor_state: Mapping[str, Any],
    expected: datetime,
    bars: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any] | None:
    """Build the causal long Q2 slow outer-failure/right-left setup.

    The function does not decide that the market is Q2 or that the setup is
    high quality.  It only joins an existing boundary reclaim event with the
    existing anchor-lifecycle reverse anchor, broken bearish Dow defense and
    right-left leg sequence.  Those facts are published for independent AI
    course assessment in LONG_Q2_Q4_ONLY mode.
    """

    records = _causal_candidate_bars(bars, expected=expected)
    if not records:
        return None
    outer_events = [
        dict(item)
        for item in structure_events
        if isinstance(item, Mapping)
        and item.get("event_type") == "FALSE_BREAK_RECLAIM"
        and item.get("direction") == "BULL"
        and item.get("source_type") in {
            "DOW_DEFENSE",
            "OR5_LOW",
            "OR15_LOW",
        }
        and isinstance(item.get("first_seen_at"), str)
        and _aware(item["first_seen_at"], "q2_slow.reclaim.first_seen_at") <= expected
    ]
    if not outer_events:
        return None
    event = max(
        outer_events,
        key=lambda item: (str(item.get("first_seen_at")), str(item.get("id"))),
    )
    reclaim_at = _aware(event["first_seen_at"], "q2_slow.reclaim_at")
    breach_at = _aware(event.get("breach_time"), "q2_slow.breach_at")
    boundary = _positive_number(event.get("level_price"), "q2_slow.boundary")
    expansion_extreme = _positive_number(
        event.get("breach_extreme"),
        "q2_slow.expansion_extreme",
    )
    if not breach_at < reclaim_at <= expected or expansion_extreme >= boundary:
        return None

    expansion_bars = [
        item
        for item in records
        if breach_at <= _aware(item["time"], "q2_slow.expansion.bar") <= reclaim_at
    ]
    if not expansion_bars:
        return None
    extreme_bar = min(
        expansion_bars,
        key=lambda item: (float(item["low"]), str(item["time"])),
    )
    if abs(float(extreme_bar["low"]) - expansion_extreme) > 1e-9:
        return None

    reverse = anchor_state.get("child_anchor")
    if not (
        isinstance(reverse, Mapping)
        and reverse.get("direction") == "BULL"
        and reverse.get("status") == "ACTIVE"
        and isinstance(reverse.get("first_seen_at"), str)
        and _aware(reverse["first_seen_at"], "q2_slow.reverse.first_seen_at") <= expected
    ):
        return None
    reverse_ref = str(reverse.get("id") or "")
    if not reverse_ref:
        return None
    reverse_origin = reverse.get("origin_price")
    if _is_positive_number(reverse_origin) and float(reverse_origin) > boundary:
        return None

    defense_options: list[Mapping[str, Any]] = []
    background = anchor_state.get("background_anchor")
    if isinstance(background, Mapping) and background.get("direction") == "BEAR":
        defense = background.get("defense")
        if isinstance(defense, Mapping):
            defense_options.append(defense)
    dow = anchor_state.get("dow_context")
    if isinstance(dow, Mapping):
        for key in ("small_bear_defense", "large_bear_defense"):
            defense = dow.get(key)
            if isinstance(defense, Mapping):
                defense_options.append(defense)
    broken_defenses = [
        item
        for item in defense_options
        if item.get("id")
        # A reclaimed bearish defense means the new bullish right side was
        # lost again; it cannot keep publishing an executable right-left plan.
        and item.get("state") == "BROKEN"
        and isinstance(item.get("broken_at"), str)
        and reclaim_at <= _aware(item["broken_at"], "q2_slow.defense.broken_at") <= expected
    ]
    if not broken_defenses:
        return None
    broken_defense = max(
        broken_defenses,
        key=lambda item: (str(item.get("broken_at")), str(item.get("id"))),
    )
    defense_broken_at = _aware(broken_defense["broken_at"], "q2_slow.defense_broken_at")

    legs = _causal_comparison_legs(
        anchor_state,
        anchor_ref=reverse_ref,
        expected=expected,
    )
    pullback: Mapping[str, Any] | None = None
    relaunch: Mapping[str, Any] | None = None
    for index, leg in enumerate(legs):
        if leg.get("direction") != "BEAR" or not isinstance(leg.get("start_time"), str):
            continue
        if _aware(leg["start_time"], "q2_slow.pullback.start") < defense_broken_at:
            continue
        prior_bull = next(
            (item for item in reversed(legs[:index]) if item.get("direction") == "BULL"),
            None,
        )
        later_bull = next(
            (item for item in legs[index + 1 :] if item.get("direction") == "BULL"),
            None,
        )
        if isinstance(prior_bull, Mapping) and isinstance(later_bull, Mapping):
            pullback = leg
            relaunch = later_bull
    if not isinstance(pullback, Mapping) or not isinstance(relaunch, Mapping):
        return None
    pullback_end = pullback.get("end_time")
    if not isinstance(pullback_end, str):
        return None
    pullback_end_at = _aware(pullback_end, "q2_slow.pullback.end")
    source_bar = next(
        (item for item in records if item["time"] == pullback_end_at.isoformat()),
        None,
    )
    if not isinstance(source_bar, Mapping):
        return None
    trigger = float(source_bar["high"])
    signal = next(
        (
            item
            for item in records
            if _aware(item["time"], "q2_slow.signal.time") > pullback_end_at
            and float(item["close"]) > trigger
        ),
        None,
    )
    if not isinstance(signal, Mapping):
        return None
    signal_at = _aware(signal["time"], "q2_slow.signal_at")
    relaunch_observed = relaunch.get("observable_at") or relaunch.get("end_time")
    if not isinstance(relaunch_observed, str) or _aware(
        relaunch_observed,
        "q2_slow.relaunch.observable_at",
    ) > signal_at:
        return None

    validity_bars = _structure_bar_count(
        records,
        start=str(pullback.get("start_time")),
        end=pullback_end,
    )
    # Candidate facts belong to the first causal signal, not the later AI
    # review tick.  Sparse/two-minute analysis may first inspect this setup one
    # or more bars after ``signal_at``; those later bars may invalidate the
    # setup, but must never rewrite its ATR buffer or protective stop.
    facts_records = [
        item
        for item in records
        if _aware(item["time"], "q2_slow.facts.bar") <= signal_at
    ]
    buffer, buffer_policy = _candidate_atr_buffer(facts_records)
    stop = round(expansion_extreme - buffer, 1)
    if any(
        _aware(item["time"], "q2_slow.post_signal.bar") > signal_at
        and float(item["low"]) <= stop
        for item in records
    ):
        return None
    pullback_ref = _candidate_leg_ref(pullback, anchor_ref=reverse_ref)
    facts_hash = _candidate_fact_hash(
        "Q2_SLOW_OUTER_EXPANSION_FAILURE",
        event.get("id"),
        event.get("source_type"),
        event.get("source_id"),
        boundary,
        breach_at.isoformat(),
        extreme_bar["time"],
        expansion_extreme,
        reclaim_at.isoformat(),
        reverse_ref,
        broken_defense.get("id"),
        broken_defense.get("broken_at"),
        pullback_ref,
        pullback_end,
        trigger,
        signal_at.isoformat(),
        buffer_policy,
        buffer,
        stop,
    )
    return {
        "setup_key": _id(
            "SETUP",
            "Q2_SLOW_OUTER_EXPANSION_FAILURE",
            str(event.get("id") or "BOUNDARY"),
            reverse_ref,
            pullback_ref,
        ),
        "setup_name": "多方Q2慢速外圍擴張失敗",
        "direction": "LONG",
        "stage": "ARMED",
        "trigger_operator": "CLOSE_ABOVE",
        "trigger_level": trigger,
        "trigger_time": signal_at.isoformat(),
        "stop_source_time": str(extreme_bar["time"]),
        "stop_source_price": expansion_extreme,
        "stop_buffer_points": buffer,
        "stop_price": stop,
        "stop_policy": "OUTER_EXPANSION_EXTREME_PLUS_CURRENT_ATR_ENGINEERING_BUFFER",
        "correction_start_time": str(pullback.get("start_time")),
        "correction_start_price": float(pullback.get("start_price")),
        "first_seen_at": signal_at.isoformat(),
        "facts_cutoff": signal_at.isoformat(),
        "facts_hash": facts_hash,
        "valid_bars": validity_bars,
        "validity_policy": {
            "kind": "STRUCTURAL",
            "basis_ref": pullback_ref,
            "invalidating_events": [
                "OUTER_EXPANSION_EXTREME_BROKEN",
                "REVERSE_ANCHOR_INVALIDATED",
                "RIGHT_LEFT_PULLBACK_INVALIDATED",
            ],
        },
        "authority": "PROGRAM_HARD_FACT_CANDIDATE",
        "decision_authority": "AI_HYBRID",
        "candidate_source": "Q2_SLOW_OUTER_EXPANSION_FAILURE",
        "entry_strategy": "Q2_SLOW_OUTER_EXPANSION_FAILURE",
        "source_event_id": event.get("id"),
        "source_type": event.get("source_type"),
        "source_id": event.get("source_id"),
        "boundary_ref": event.get("source_id"),
        "reverse_anchor_ref": reverse_ref,
        "broken_defense_ref": broken_defense.get("id"),
        "right_left_pullback_ref": pullback_ref,
        "engineering_buffer_policy": buffer_policy,
        "behavior_policy": "Q2_SLOW_OUTER_FAILURE_STRUCTURE_REVIEW",
        # This is an execution behavior review window, not an anchor/quadrant
        # classifier. The course contract expects Q2 to leave the outer edge
        # promptly, while its structural setup validity remains event-based.
        "behavior_max_wait_bars": min(3, max(2, validity_bars)),
        "behavior_trigger_level": boundary,
        "behavior_obstacles": [
            {
                "price": trigger,
                "role": "TRIGGER",
                "source_time": pullback_end,
                "source_field": "high",
            },
            {
                "price": boundary,
                "role": "HOLD_BOUNDARY",
                "source_time": str(event.get("first_seen_at")),
                "source_field": "close",
            },
        ],
    }


def _dragon_quality(bars: list[dict[str, Any]], direction: str) -> str | None:
    if len(bars) < 3:
        return None
    pairs = list(zip(bars, bars[1:]))
    favorable = 0
    for left, right in pairs:
        if direction == "BULL":
            ordered = float(right["high"]) > float(left["high"]) and float(right["low"]) >= float(left["low"])
        else:
            ordered = float(right["low"]) < float(left["low"]) and float(right["high"]) <= float(left["high"])
        favorable += int(ordered)
    if favorable == len(pairs):
        return "GOLD_DRAGON"
    if favorable * 2 >= len(pairs):
        return "K_GOLD_DRAGON"
    return None


def _derive_left_right_state(anchor_state: Mapping[str, Any]) -> dict[str, Any]:
    background = anchor_state.get("background_anchor")
    child = anchor_state.get("child_anchor")
    reverse = anchor_state.get("reverse_candidate")
    dow = anchor_state.get("dow_context")
    background_direction = background.get("direction") if isinstance(background, Mapping) else None
    if (
        isinstance(child, Mapping)
        and background_direction in {"BULL", "BEAR"}
        and child.get("direction") != background_direction
        and child.get("status") == "ACTIVE"
    ):
        state = "RIGHT_CONFIRMED"
        direction = child.get("direction")
        reasons = ["OPPOSITE_CHILD_ANCHOR_ACTIVE"]
    elif isinstance(reverse, Mapping) and reverse.get("status") in {"QUALIFIED", "AWAITING_CONTINUATION"}:
        state = "LEFT_CANDIDATE"
        direction = reverse.get("direction")
        reasons = ["REVERSE_CANDIDATE_NOT_YET_TAKEOVER"]
    elif (
        isinstance(background, Mapping)
        and isinstance(dow, Mapping)
        and dow.get("small_state") in {"BULL", "BEAR"}
        and dow.get("small_state") != background.get("direction")
    ):
        state = "RIGHT_CONFIRMING"
        direction = dow.get("small_state")
        reasons = ["SMALL_DOW_OPPOSES_BACKGROUND"]
    else:
        state = "NONE"
        direction = None
        reasons = ["NO_REVERSAL_SEQUENCE"]
    return {
        "state": state,
        "direction": direction,
        "reason_codes": reasons,
    }


def _continuation_arm_candidate(
    anchor_state: Mapping[str, Any],
    *,
    expected: datetime,
    bars: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Expose an objective pullback-continuation trigger once its correction ends.

    This is not an entry decision.  It prevents the analyzer from waiting an
    extra full swing after a confirmed counter-direction correction: the
    current same-direction working extreme becomes a future close trigger and
    the confirmed correction endpoint remains the structural stop source.
    """

    background = anchor_state.get("background_anchor")
    child_anchor = anchor_state.get("child_anchor")
    reverse = anchor_state.get("reverse_candidate")
    working = anchor_state.get("working_leg")
    controller = (
        background
        if isinstance(background, Mapping) and background.get("status") in {None, "ACTIVE"}
        else child_anchor
        if isinstance(child_anchor, Mapping) and child_anchor.get("status") in {None, "ACTIVE"}
        else None
    )
    if not isinstance(controller, Mapping):
        return None

    quadrant = anchor_state.get("quadrant_context")
    comparisons: Mapping[str, Any] | None = None
    if isinstance(quadrant, Mapping):
        same_grade = quadrant.get("same_grade_comparisons")
        child_grade = quadrant.get("child_comparisons")
        controller_ref = controller.get("id")
        for item in (same_grade, child_grade):
            if (
                isinstance(item, Mapping)
                and (
                    item.get("anchor_ref") == controller_ref
                    or comparisons is None
                )
            ):
                comparisons = item
                if item.get("anchor_ref") == controller_ref:
                    break
    raw_legs = comparisons.get("legs") if isinstance(comparisons, Mapping) else None
    legs = [item for item in raw_legs if isinstance(item, Mapping)] if isinstance(raw_legs, list) else []

    direction: str | None = None
    start_time: Any = None
    start_price: Any = None
    trigger_time: Any = None
    trigger_price: Any = None
    previous_correction: Mapping[str, Any] | None = None

    role = str(working.get("role") or "") if isinstance(working, Mapping) else ""
    if isinstance(working, Mapping) and role in {
        "BACKGROUND_RETEST",
        "REVERSE_CANDIDATE_PULLBACK",
        "REVERSE_CANDIDATE_CONTINUATION",
    }:
        if role in {"BACKGROUND_RETEST", "REVERSE_CANDIDATE_PULLBACK"} and (
            working.get("direction") == controller.get("direction")
        ):
            direction = str(controller.get("direction") or "")
        elif (
            isinstance(background, Mapping)
            and isinstance(child_anchor, Mapping)
            and isinstance(reverse, Mapping)
            and child_anchor.get("status") == "ACTIVE"
            and reverse.get("status") == "QUALIFIED"
            and child_anchor.get("direction") == reverse.get("direction")
        ):
            direction = str(child_anchor.get("direction") or "")
        if direction in {"BULL", "BEAR"} and working.get("direction") == direction:
            start_time = working.get("start_time")
            start_price = working.get("start_price")
            trigger_time = working.get("current_extreme_time")
            trigger_price = working.get("current_extreme_price")
            previous_correction = next(
                (
                    item
                    for item in reversed(legs)
                    if item.get("end_time") == start_time
                    and item.get("direction") != direction
                    and item.get("status") == "LOCAL_CONFIRMED"
                ),
                None,
            )

    # n=2 can confirm a correction endpoint on the same closed bar that also
    # crosses the preceding push high/low.  Once the new push is absorbed into
    # the extended anchor, working_leg may be null; recover that causal setup
    # from the final parent-correction-current sequence without waiting for an
    # unnecessary extra swing.
    if not isinstance(previous_correction, Mapping) and len(legs) >= 3:
        parent, correction, current = legs[-3:]
        fallback_direction = str(current.get("direction") or "")
        child_defense = (
            child_anchor.get("defense") if isinstance(child_anchor, Mapping) else None
        )
        child_allows_background = (
            not isinstance(child_anchor, Mapping)
            or child_anchor.get("direction") == fallback_direction
            or (
                child_anchor.get("direction") != fallback_direction
                and child_anchor.get("status") in {"DEGRADED", "DEGRADED_RECLAIMED"}
                and isinstance(child_defense, Mapping)
                and child_defense.get("broken_at")
            )
        )
        sequence_is_causal = (
            fallback_direction == controller.get("direction")
            and child_allows_background
            and parent.get("direction") == fallback_direction
            and correction.get("direction") != fallback_direction
            and current.get("start_time") == correction.get("end_time")
            and correction.get("start_time") == parent.get("end_time")
            and parent.get("status") == "LOCAL_CONFIRMED"
            and correction.get("status") == "LOCAL_CONFIRMED"
            and current.get("status") == "FORMING"
        )
        if sequence_is_causal:
            direction = fallback_direction
            start_time = current.get("start_time")
            start_price = current.get("start_price")
            trigger_time = parent.get("end_time")
            trigger_price = parent.get("end_price")
            previous_correction = correction

    lifecycle_candidate = None
    if (
        direction in {"BULL", "BEAR"}
        and isinstance(previous_correction, Mapping)
        and isinstance(start_time, str)
        and isinstance(trigger_time, str)
        and not any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0
            for value in (start_price, trigger_price)
        )
        and _aware(trigger_time, "continuation.trigger_time") <= expected
    ):
        side = "LONG" if direction == "BULL" else "SHORT"
        operator = "CLOSE_ABOVE" if direction == "BULL" else "CLOSE_BELOW"
        lifecycle_candidate = {
            "setup_key": _id("SETUP", "PULLBACK_CONTINUATION", side, start_time),
            "setup_name": "多方修正後複製" if direction == "BULL" else "空方修正後複製",
            "direction": side,
            "stage": "ARMED",
            "trigger_operator": operator,
            "trigger_level": float(trigger_price),
            "trigger_time": trigger_time,
            "stop_source_time": start_time,
            "stop_source_price": float(start_price),
            "correction_start_time": previous_correction.get("start_time"),
            "correction_start_price": previous_correction.get("start_price"),
            "first_seen_at": expected.isoformat(),
            "valid_bars": 5,
            "authority": "PROGRAM_CANDIDATE_NOT_AUTOMATIC_ENTRY",
            "candidate_source": "ANCHOR_LEG_SEQUENCE",
            "entry_strategy": "Q4_PULLBACK_CONTINUATION",
        }
        lifecycle_candidate = _attach_program_continuation_stop(
            lifecycle_candidate,
            bars=bars,
        )

    # An outside reversal candle can be both the push extreme and the correction
    # extreme.  The course pivot chain deliberately excludes it because OHLC
    # cannot prove intrabar high/low order.  That is correct for Dow structure,
    # but it used to make an already active Q4 trend wait for a much later full
    # Dow swing before it could arm again.  For execution we can use the candle's
    # opposite extreme after two *later* bars hold it: no intrabar order is
    # invented, the endpoint is first observable only at the second right bar,
    # and the trigger comes solely from bars after that endpoint candle.
    endpoint_candidate = _confirmed_pullback_endpoint_candidate(
        controller,
        bars=bars,
        expected=expected,
    )
    if endpoint_candidate is None:
        return lifecycle_candidate
    if lifecycle_candidate is None:
        return endpoint_candidate
    if _aware(
        endpoint_candidate["stop_source_time"],
        "endpoint_candidate.stop_source_time",
    ) > _aware(
        lifecycle_candidate["stop_source_time"],
        "lifecycle_candidate.stop_source_time",
    ):
        return endpoint_candidate
    return lifecycle_candidate


def _active_grade_upgrade_controller(
    structure_events: Sequence[Mapping[str, Any]] | None,
    structural_legs: Sequence[Mapping[str, Any]] | None,
    *,
    anchor_state: Mapping[str, Any],
    expected: datetime,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    """Project an active grade upgrade as control evidence, not a formal anchor.

    A SMALL -> LARGE upgrade can own the large structural grade before the
    anchor lifecycle has enough same-grade turns to install a formal large
    anchor.  The distinction matters: the program may use the promoted
    structure to expose a Q4 candidate, but it must not write a synthetic
    ``background_anchor`` into the authoritative anchor lifecycle.
    """

    events = [
        dict(item)
        for item in (structure_events or [])
        if isinstance(item, Mapping)
        and isinstance(item.get("first_seen_at"), str)
        and _aware(item["first_seen_at"], "q4_upgrade.event.first_seen_at") <= expected
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
        and item.get("direction") == "BULL"
        and item.get("from_level") == "SMALL"
        and item.get("to_level") == "LARGE"
        and str(item.get("id") or "") not in superseded
        and item.get("source_anchor_id")
        and item.get("replacement_defense_pivot_id")
        and isinstance(item.get("replacement_defense_time"), str)
        and _is_positive_number(item.get("replacement_defense_price"))
        and isinstance(item.get("parent_origin_time"), str)
        and _is_positive_number(item.get("parent_origin_price"))
    ]
    if not active:
        return None

    formal_background = anchor_state.get("background_anchor")
    if (
        isinstance(formal_background, Mapping)
        and formal_background.get("status") in {None, "ACTIVE", "DEGRADED", "DEGRADED_RECLAIMED"}
        and formal_background.get("direction") == "BEAR"
    ):
        # A local opposite upgrade is not allowed to overrule an already
        # installed, still-retained large controller.
        return None

    event = max(
        active,
        key=lambda item: (str(item.get("first_seen_at")), str(item.get("id"))),
    )
    controller_ref = str(event["source_anchor_id"])
    controller = {
        "id": controller_ref,
        "direction": "BULL",
        "level": "LARGE",
        "status": "ACTIVE",
        "origin_time": str(event["parent_origin_time"]),
        "origin_price": float(event["parent_origin_price"]),
        "first_seen_at": str(event["first_seen_at"]),
        "source": "GRADE_UPGRADE_CONTROL_EVIDENCE",
        "source_event_id": str(event["id"]),
    }
    defense = {
        "id": str(event["replacement_defense_pivot_id"]),
        "direction": "BULL",
        "level": "LARGE",
        "time": str(event["replacement_defense_time"]),
        "price": float(event["replacement_defense_price"]),
        "state": "ACTIVE",
        "first_seen_at": str(event["first_seen_at"]),
        "source_event_id": str(event["id"]),
    }
    legs = [
        dict(item)
        for item in (structural_legs or [])
        if isinstance(item, Mapping)
        and isinstance(item.get("end_time"), str)
        and _aware(item["end_time"], "q4_upgrade.leg.end_time") <= expected
        and isinstance(item.get("first_seen_at"), str)
        and _aware(item["first_seen_at"], "q4_upgrade.leg.first_seen_at") <= expected
    ]
    return controller, defense, legs


def _q4_aggressive_pullback_reversal_candidate(
    anchor_state: Mapping[str, Any],
    *,
    expected: datetime,
    bars: Sequence[Mapping[str, Any]] | None,
    structure_events: Sequence[Mapping[str, Any]] | None = None,
    structural_legs: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Expose a causal long Q4 stop-and-turn without waiting for n=2.

    This builder publishes only an execution fact candidate.  It requires an
    existing bullish anchor and active same-grade bullish Dow defense from the
    anchor lifecycle, then observes a bearish correction and the first closed
    bar that turns above the immediately preceding counter bar.  Q4, Taiji,
    location quality and trade quality remain AI judgements.
    """

    records = _causal_candidate_bars(bars, expected=expected)
    if len(records) < 2:
        return None

    controllers = [
        item
        for item in (anchor_state.get("child_anchor"), anchor_state.get("background_anchor"))
        if isinstance(item, Mapping)
        and item.get("direction") == "BULL"
        and item.get("status") in {None, "ACTIVE"}
        and isinstance(item.get("first_seen_at"), str)
        and _aware(item["first_seen_at"], "q4_aggressive.controller.first_seen_at") <= expected
    ]
    controller: Mapping[str, Any] | None = None
    defense: Mapping[str, Any] | None = None
    comparison_legs: list[dict[str, Any]] = []
    for option in controllers:
        option_ref = str(option.get("id") or "")
        if not option_ref:
            continue
        dow = anchor_state.get("dow_context")
        grade = str(option.get("level") or "SMALL").lower()
        defense_candidates = [
            item
            for item in (
                (
                    dow.get(f"{grade}_bull_defense")
                    if isinstance(dow, Mapping)
                    else None
                ),
                option.get("defense"),
            )
            if isinstance(item, Mapping)
            and item.get("id")
            and item.get("direction") in {None, "BULL"}
            and item.get("state") in {None, "ACTIVE"}
            and isinstance(item.get("first_seen_at"), str)
            and _aware(
                item["first_seen_at"],
                "q4_aggressive.defense.first_seen_at",
            ) <= expected
        ]
        if not defense_candidates:
            continue
        option_defense = max(
            defense_candidates,
            key=lambda item: (str(item.get("first_seen_at")), str(item.get("id"))),
        )
        legs = _causal_comparison_legs(
            anchor_state,
            anchor_ref=option_ref,
            expected=expected,
        )
        if legs:
            controller = option
            defense = option_defense
            comparison_legs = legs
            break
    controller_event_ref: str | None = None
    if not isinstance(controller, Mapping) or not isinstance(defense, Mapping):
        promoted = _active_grade_upgrade_controller(
            structure_events,
            structural_legs,
            anchor_state=anchor_state,
            expected=expected,
        )
        if promoted is not None:
            controller, defense, comparison_legs = promoted
            controller_event_ref = str(controller.get("source_event_id") or "") or None
    if not isinstance(controller, Mapping) or not isinstance(defense, Mapping):
        return None

    working = anchor_state.get("working_leg")
    active_bear_correction = bool(
        isinstance(working, Mapping)
        and working.get("direction") == "BEAR"
        and isinstance(working.get("start_time"), str)
        and isinstance(working.get("first_seen_at"), str)
        and _aware(working["start_time"], "q4_aggressive.correction.start") <= expected
        and _aware(working["first_seen_at"], "q4_aggressive.correction.first_seen_at") <= expected
    )
    controller_ref = str(controller.get("id"))
    eligible_parent_pushes = [
        item
        for item in comparison_legs
        if item.get("direction") == "BULL"
        and item.get("source") != "GRADE_UPGRADE"
        and item.get("status") in {"CONFIRMED", "LOCAL_CONFIRMED"}
        and isinstance(item.get("end_time"), str)
        and _aware(item["end_time"], "q4_aggressive.parent.end") <= expected
    ]
    if active_bear_correction:
        correction_start = _aware(
            str(working["start_time"]),
            "q4_aggressive.correction_start",
        )
        eligible_parent_pushes = [
            item
            for item in eligible_parent_pushes
            if _aware(item["end_time"], "q4_aggressive.parent.end") <= correction_start
        ]
    elif eligible_parent_pushes:
        latest_parent = max(
            eligible_parent_pushes,
            key=lambda item: (str(item.get("end_time")), str(item.get("id"))),
        )
        correction_start = _aware(
            str(latest_parent["end_time"]),
            "q4_aggressive.correction_start",
        )
    else:
        return None
    parent_push = max(
        eligible_parent_pushes,
        key=lambda item: (str(item.get("end_time")), str(item.get("id"))),
        default=None,
    )
    if not isinstance(parent_push, Mapping):
        return None

    correction_bars = [
        item
        for item in records
        if _aware(item["time"], "q4_aggressive.correction.bar") >= correction_start
    ]
    if len(correction_bars) < 2:
        return None
    defense_price = _positive_number(defense.get("price"), "q4_aggressive.defense.price")
    # A course Q4 relaunch is not restricted to an adjacent red/green pair.
    # Accept a closed bullish bar that clears the whole micro-base beginning at
    # the most recent counter bar.  Q4/TAIJI quality remains an AI judgement;
    # the program publishes only the causal reaction line and correction low.
    signal_options: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for index, current in enumerate(correction_bars[1:], start=1):
        if not (
            float(current["close"]) > float(current["open"])
        ):
            continue
        prior = correction_bars[:index]
        counter_indices = [
            prior_index
            for prior_index, item in enumerate(prior)
            if float(item["close"]) < float(item["open"])
        ]
        if not counter_indices:
            continue
        micro_base = prior[counter_indices[-1] :]
        trigger_source = max(
            micro_base,
            key=lambda item: (float(item["high"]), str(item["time"])),
        )
        if float(current["close"]) > float(trigger_source["high"]):
            signal_options.append((trigger_source, current))
    if not signal_options:
        return None

    selected_signal: tuple[
        Mapping[str, Any],
        Mapping[str, Any],
        datetime,
        list[dict[str, Any]],
        Mapping[str, Any],
        float,
        float,
        int,
        float,
        str,
        float,
    ] | None = None
    for trigger_source, signal in signal_options:
        signal_at = _aware(signal["time"], "q4_aggressive.signal_at")
        through_signal = [
            item
            for item in correction_bars
            if _aware(item["time"], "q4_aggressive.running_extreme.bar") <= signal_at
        ]
        # The course defense is close-qualified. A wick may test it, but a
        # closed break contradicts the still-active lifecycle defense.
        if any(float(item["close"]) < defense_price for item in through_signal):
            continue
        running_low_bar = min(
            through_signal,
            key=lambda item: (float(item["low"]), str(item["time"])),
        )
        running_low = float(running_low_bar["low"])
        trigger = float(trigger_source["high"])
        validity_bars = _structure_bar_count(
            records,
            start=correction_start.isoformat(),
            end=signal_at.isoformat(),
        )
        # Freeze engineering facts at the first closed signal. Bars arriving
        # before a slower AI review may retire the setup, but cannot improve or
        # worsen the already published stop/facts identity.
        facts_records = [
            item
            for item in records
            if _aware(item["time"], "q4_aggressive.facts.bar") <= signal_at
        ]
        buffer, buffer_policy = _candidate_atr_buffer(facts_records)
        stop = round(running_low - buffer, 1)
        if any(
            _aware(item["time"], "q4_aggressive.post_signal.bar") > signal_at
            and float(item["low"]) <= stop
            for item in records
        ):
            # The first attempt may fail while the parent controller survives.
            # A later micro-base/relaunch then owns a new setup identity.
            continue
        selected_signal = (
            trigger_source,
            signal,
            signal_at,
            through_signal,
            running_low_bar,
            running_low,
            trigger,
            validity_bars,
            buffer,
            buffer_policy,
            stop,
        )
        break
    if selected_signal is None:
        return None
    (
        trigger_source,
        signal,
        signal_at,
        through_signal,
        running_low_bar,
        running_low,
        trigger,
        validity_bars,
        buffer,
        buffer_policy,
        stop,
    ) = selected_signal
    parent_ref = _candidate_leg_ref(parent_push, anchor_ref=controller_ref)
    facts_hash = _candidate_fact_hash(
        "Q4_AGGRESSIVE_PULLBACK_REVERSAL",
        controller_ref,
        controller_event_ref,
        defense.get("id"),
        parent_ref,
        correction_start.isoformat(),
        trigger_source["time"],
        trigger,
        signal_at.isoformat(),
        running_low_bar["time"],
        running_low,
        buffer_policy,
        buffer,
        stop,
    )
    return {
        "setup_key": _id(
            "SETUP",
            "Q4_AGGRESSIVE_PULLBACK_REVERSAL",
            controller_ref,
            correction_start.isoformat(),
            signal_at.isoformat(),
        ),
        "setup_name": "多方Q4積極型拉回反轉",
        "direction": "LONG",
        "stage": "ARMED",
        "trigger_operator": "CLOSE_ABOVE",
        "trigger_level": trigger,
        "trigger_time": signal_at.isoformat(),
        "stop_source_time": str(running_low_bar["time"]),
        "stop_source_price": running_low,
        "stop_buffer_points": buffer,
        "stop_price": stop,
        "stop_policy": "CAUSAL_SO_FAR_CORRECTION_EXTREME_PLUS_CURRENT_ATR_ENGINEERING_BUFFER",
        "correction_start_time": correction_start.isoformat(),
        "correction_start_price": float(parent_push.get("end_price")),
        "first_seen_at": signal_at.isoformat(),
        # Freeze the candidate's identity at its first causal signal. Later
        # review ticks may add evidence, but they must not silently mutate the
        # original setup, stop or facts hash.
        "facts_cutoff": signal_at.isoformat(),
        "evaluated_at": expected.isoformat(),
        "facts_hash": facts_hash,
        # The correction may span far more than the executable signal window.
        # Keep those two clocks separate: evidence_span_bars is descriptive,
        # while valid_bars bounds how long a confirmed entry may wait to fill.
        "evidence_span_bars": validity_bars,
        "valid_bars": 3,
        "validity_policy": {
            "kind": "STRUCTURAL",
            "basis_ref": (
                _candidate_leg_ref(working, anchor_ref=controller_ref)
                if active_bear_correction and isinstance(working, Mapping)
                else parent_ref
            ),
            "invalidating_events": [
                "CORRECTION_EXTREME_BROKEN",
                "CONTROLLER_DEFENSE_BROKEN",
                "CONTROLLER_ANCHOR_REPLACED",
            ],
        },
        "authority": "PROGRAM_HARD_FACT_CANDIDATE",
        "decision_authority": "AI_HYBRID",
        "candidate_source": "Q4_AGGRESSIVE_PULLBACK_REVERSAL",
        "entry_strategy": "Q4_AGGRESSIVE_PULLBACK_REVERSAL",
        "confirmation_style": "AGGRESSIVE",
        "stop_extreme_status": "CAUSAL_SO_FAR_NOT_N2",
        "controller_anchor_ref": controller_ref,
        "controller_structure_event_ref": controller_event_ref,
        "controller_defense_ref": defense.get("id"),
        "parent_push_ref": parent_ref,
        "observation_zone_refs": [defense.get("id")],
        "engineering_buffer_policy": buffer_policy,
        "behavior_policy": "Q4_AGGRESSIVE_STRUCTURE_AND_COPY_REVIEW",
        # Q4 can progress more slowly than Q2. This bounded review window is
        # only post-entry behavior management; it does not define the anchor,
        # structure grade, quadrant or setup validity.
        "behavior_max_wait_bars": min(5, max(3, validity_bars)),
        "behavior_trigger_level": trigger,
        "behavior_obstacles": [
            {
                "price": trigger,
                "role": "TRIGGER",
                "source_time": str(trigger_source["time"]),
                "source_field": "high",
            },
            {
                "price": float(parent_push.get("end_price")),
                "role": "CHECKPOINT",
                "source_time": str(parent_push.get("end_time")),
                "source_field": "high",
            },
        ],
    }


def _attach_program_continuation_stop(
    candidate: dict[str, Any],
    *,
    bars: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Lock a reproducible stop outside the setup's own correction endpoint."""

    if not bars:
        return candidate
    records = [dict(item) for item in bars if isinstance(item, Mapping)]
    atr = _atr_from_bar_records(records, 14) if len(records) >= 15 else None
    # The session is intentionally independent from its reference session, so
    # ATR14 does not exist during the first 14 bars.  A structural stop can
    # still be locked outside the confirmed correction: use one TMF point
    # until ATR14 is available, then switch to the documented 0.2 ATR buffer.
    buffer = round(max(1.0, float(atr) * 0.2), 1) if atr is not None else 1.0
    source = _positive_number(candidate.get("stop_source_price"), "candidate.stop_source_price")
    direction = str(candidate.get("direction") or "")
    if direction == "LONG":
        stop = source - buffer
    elif direction == "SHORT":
        stop = source + buffer
    else:
        return candidate
    trigger = _positive_number(candidate.get("trigger_level"), "candidate.trigger_level")
    checkpoint = _positive_number(
        candidate.get("correction_start_price"),
        "candidate.correction_start_price",
    )
    trigger_time = str(candidate.get("trigger_time") or "")
    checkpoint_time = str(candidate.get("correction_start_time") or "")
    obstacles = [
        {
            "price": trigger,
            "role": "TRIGGER",
            "source_time": trigger_time,
            "source_field": "high" if direction == "LONG" else "low",
        }
    ]
    checkpoint_is_favorable = (
        direction == "LONG" and checkpoint > trigger
    ) or (
        direction == "SHORT" and checkpoint < trigger
    )
    if checkpoint_is_favorable:
        obstacles.append(
            {
                "price": checkpoint,
                "role": "CHECKPOINT",
                "source_time": checkpoint_time,
                "source_field": "high" if direction == "LONG" else "low",
            }
        )
    return {
        **candidate,
        "stop_buffer_points": buffer,
        "stop_price": round(stop, 1),
        "stop_policy": (
            "COMPLETE_CORRECTION_EXTREME_PLUS_0_2_ATR_ENGINEERING_BUFFER"
            if atr is not None
            else "COMPLETE_CORRECTION_EXTREME_PLUS_ONE_POINT_PRE_ATR14_BUFFER"
        ),
        "decision_authority": "PROGRAM",
        "behavior_max_wait_bars": 5,
        "behavior_trigger_level": trigger,
        "behavior_obstacles": obstacles,
    }


def _false_break_arm_candidate(
    structure_events: Sequence[Mapping[str, Any]],
    *,
    expected: datetime,
    bars: Sequence[Mapping[str, Any]] | None,
    include_causal_checkpoint: bool = False,
) -> dict[str, Any] | None:
    """Turn the latest causal false-break reclaim into a fixed entry plan.

    The reclaim close only arms the setup.  Course v2.1.8 requires a later
    closed bar to break the reclaim-confirmation candle's favorable extreme
    before the setup becomes entry-eligible.  The sweep extreme, not a later
    model-selected pivot, owns the protective stop.
    """

    events = [
        item
        for item in structure_events
        if isinstance(item, Mapping)
        and item.get("event_type") == "FALSE_BREAK_RECLAIM"
        and isinstance(item.get("first_seen_at"), str)
        and _aware(item["first_seen_at"], "false_break.first_seen_at") <= expected
    ]
    if not events or not bars:
        return None
    event = max(events, key=lambda item: (str(item.get("first_seen_at")), str(item.get("id"))))
    direction = str(event.get("direction") or "")
    if direction not in {"BULL", "BEAR"}:
        return None
    records = _causal_candidate_bars(bars, expected=expected)
    level = _positive_number(event.get("level_price"), "false_break.level_price")
    extreme = _positive_number(event.get("breach_extreme"), "false_break.breach_extreme")
    side = "LONG" if direction == "BULL" else "SHORT"
    first_seen = str(event["first_seen_at"])
    first_seen_at = _aware(first_seen, "false_break.first_seen_at")
    facts_records = [
        item
        for item in records
        if _aware(item["time"], "false_break.facts.bar") <= first_seen_at
    ]
    atr = _atr_from_bar_records(facts_records, 14) if len(facts_records) >= 15 else None
    buffer = round(max(1.0, float(atr) * 0.2), 1) if atr is not None else 1.0
    stop = extreme - buffer if side == "LONG" else extreme + buffer
    reclaim_bar = next(
        (
            item
            for item in records
            if str(item.get("time") or "") == first_seen
        ),
        None,
    )
    if not isinstance(reclaim_bar, Mapping):
        return None
    confirmation_level = _positive_number(
        reclaim_bar.get("high") if side == "LONG" else reclaim_bar.get("low"),
        "false_break.reclaim_confirmation_extreme",
    )
    obstacles = [
        {
            "price": confirmation_level,
            "role": "TRIGGER",
            "source_time": first_seen,
            "source_field": "high" if side == "LONG" else "low",
        }
    ]
    if include_causal_checkpoint:
        # Long-only AI hybrid entries must carry the course-native inspection
        # point into execution. Otherwise the behavior auditor has to invent a
        # generic 0.25R threshold that can contradict the accepted setup text.
        # Use only a pivot already confirmed at this cutoff; never a later bar.
        signal_close = _positive_number(event.get("reclaim_close"), "false_break.reclaim_close")
        pivots = _working_pivot_group(facts_records, expected=first_seen_at)
        favorable = [
            item
            for item in pivots
            if (
                side == "LONG"
                and item.get("kind") == "HIGH"
                and float(item.get("price")) > signal_close
            )
            or (
                side == "SHORT"
                and item.get("kind") == "LOW"
                and float(item.get("price")) < signal_close
            )
        ]
        checkpoint = (
            min(favorable, key=lambda item: (float(item["price"]), str(item.get("bar_time"))))
            if side == "LONG" and favorable
            else max(favorable, key=lambda item: (float(item["price"]), str(item.get("bar_time"))))
            if side == "SHORT" and favorable
            else None
        )
        if isinstance(checkpoint, Mapping):
            obstacles.append(
                {
                    "price": float(checkpoint["price"]),
                    "role": "CHECKPOINT",
                    "source_time": str(checkpoint.get("bar_time") or first_seen),
                    "source_field": "high" if side == "LONG" else "low",
                }
            )
    buffer_policy = (
        "FALSE_BREAK_EXTREME_PLUS_0_2_ATR_ENGINEERING_BUFFER"
        if atr is not None
        else "FALSE_BREAK_EXTREME_PLUS_ONE_POINT_PRE_ATR14_BUFFER"
    )
    facts_hash = _candidate_fact_hash(
        "FALSE_BREAK_RECLAIM",
        side,
        event.get("id"),
        event.get("source_type"),
        event.get("source_id"),
        level,
        str(event.get("breach_time") or first_seen),
        extreme,
        event.get("reclaim_close"),
        first_seen,
        confirmation_level,
        tuple(
            (
                item.get("role"),
                item.get("price"),
                item.get("source_time"),
                item.get("source_field"),
            )
            for item in obstacles
        ),
        buffer_policy,
        buffer,
        round(stop, 1),
    )
    return {
        "setup_key": _id("SETUP", "FALSE_BREAK_RECLAIM", side, str(event.get("id"))),
        "setup_name": "多方假跌破收復" if side == "LONG" else "空方假突破跌回",
        "direction": side,
        "stage": "ARMED",
        # The reclaim candle itself cannot cross its own high/low by close.
        # Therefore CLOSE_* can only fire on a later causal closed bar.
        "trigger_operator": "CLOSE_ABOVE" if side == "LONG" else "CLOSE_BELOW",
        "trigger_level": confirmation_level,
        "trigger_time": first_seen,
        "reclaim_boundary_level": level,
        "reclaim_confirmation_time": first_seen,
        "reclaim_confirmation_extreme": confirmation_level,
        "stop_source_time": str(event.get("breach_time") or first_seen),
        "stop_source_price": extreme,
        "correction_start_time": str(event.get("breach_time") or first_seen),
        "correction_start_price": level,
        "first_seen_at": first_seen,
        "facts_cutoff": first_seen,
        "facts_hash": facts_hash,
        "valid_bars": 3,
        "authority": "PROGRAM_CANDIDATE",
        "candidate_source": "FALSE_BREAK_RECLAIM",
        "entry_strategy": "Q2_FALSE_BREAK_RECLAIM",
        "q2_reclaim_bars": int(event.get("bars_to_reclaim") or 0),
        "source_event_id": event.get("id"),
        "source_type": event.get("source_type"),
        "source_id": event.get("source_id"),
        "stop_buffer_points": buffer,
        "stop_price": round(stop, 1),
        "stop_policy": buffer_policy,
        "decision_authority": "PROGRAM",
        "behavior_policy": (
            "FALSE_BREAK_RECLAIM_CAUSAL_CHECKPOINT"
            if include_causal_checkpoint
            else None
        ),
        "behavior_max_wait_bars": 3,
        "behavior_trigger_level": confirmation_level,
        "behavior_obstacles": obstacles,
    }


def _opening_range_pullback_candidate(
    opening_ranges: Any,
    *,
    expected: datetime,
    bars: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any] | None:
    """Build a causal OR breakout -> first retest -> relaunch plan.

    One-minute OHLC cannot reproduce the course's tape/level-2 test.  This
    deterministic proxy therefore requires a directional real-body breakout,
    an actual retest of the known boundary without a closing failure, and a
    later close through the retest mini-range.  The exact proxy evidence is
    retained on the candidate for audit and replay evaluation.
    """

    if not isinstance(opening_ranges, Mapping) or not bars:
        return None
    records = [dict(item) for item in bars if isinstance(item, Mapping)]
    records.sort(key=lambda item: str(item.get("time") or ""))
    records = [
        item for item in records
        if item.get("time") and _aware(item["time"], "or.bar.time") <= expected
    ]
    if len(records) < 7:
        return None
    atr = _atr_from_bar_records(records, 14) if len(records) >= 15 else None
    touch_buffer = max(1.0, float(atr) * 0.2) if atr is not None else 1.0
    candidates: list[dict[str, Any]] = []

    for name in ("or5", "or15"):
        opening = opening_ranges.get(name)
        if not isinstance(opening, Mapping):
            continue
        start_text = str(opening.get("start") or "")
        end_text = str(opening.get("end") or "")
        if not start_text or not end_text:
            continue
        start_at = _aware(start_text, f"{name}.start")
        end_at = _aware(end_text, f"{name}.end")
        # The formal replay rule only grants the early OR5 setup to the day
        # session.  Night and US-open segments use their completed OR15.
        if name == "or5" and start_at.time() != time(8, 45):
            continue
        high = _positive_number(opening.get("high"), f"{name}.high")
        low = _positive_number(opening.get("low"), f"{name}.low")
        if high <= low:
            continue
        range_bars = [
            item for item in records
            if start_at <= _aware(item["time"], "or.range.time") <= end_at
        ]
        later = [
            item for item in records
            if _aware(item["time"], "or.later.time") > end_at
        ]
        if len(range_bars) < (5 if name == "or5" else 15) or len(later) < 2:
            continue
        for direction in ("BULL", "BEAR"):
            level = high if direction == "BULL" else low
            boundary_bar = (
                max(range_bars, key=lambda item: float(item["high"]))
                if direction == "BULL"
                else min(range_bars, key=lambda item: float(item["low"]))
            )
            boundary_time = str(boundary_bar["time"])
            breakout_index = next(
                (
                    index for index, item in enumerate(later)
                    if _directional_breakout_bar(item, direction=direction, level=level)
                ),
                None,
            )
            if breakout_index is None:
                continue
            breakout = later[breakout_index]
            breakout_at = _aware(breakout["time"], "or.breakout.time")
            if name == "or5" and breakout_at > start_at + timedelta(minutes=14):
                continue

            retest_index: int | None = None
            failed = False
            for index in range(breakout_index + 1, len(later)):
                item = later[index]
                close = float(item["close"])
                if (direction == "BULL" and close < level) or (
                    direction == "BEAR" and close > level
                ):
                    failed = True
                    break
                touches = (
                    float(item["low"]) <= level + touch_buffer
                    if direction == "BULL"
                    else float(item["high"]) >= level - touch_buffer
                )
                if touches:
                    retest_index = index
                    break
            if failed or retest_index is None:
                continue

            relaunch_index: int | None = None
            for index in range(retest_index + 1, len(later)):
                item = later[index]
                close = float(item["close"])
                if (direction == "BULL" and close < level) or (
                    direction == "BEAR" and close > level
                ):
                    failed = True
                    break
                mini = later[breakout_index + 1:index]
                if not mini:
                    continue
                mini_edge = (
                    max(float(value["high"]) for value in mini)
                    if direction == "BULL"
                    else min(float(value["low"]) for value in mini)
                )
                directional_close = (
                    close > float(item["open"])
                    if direction == "BULL"
                    else close < float(item["open"])
                )
                relaunched = (
                    close > mini_edge if direction == "BULL" else close < mini_edge
                )
                if directional_close and relaunched:
                    relaunch_index = index
                    break
            if failed or relaunch_index is None:
                continue
            relaunch = later[relaunch_index]
            relaunch_at = _aware(relaunch["time"], "or.relaunch.time")
            if name == "or5" and relaunch_at > start_at + timedelta(minutes=14):
                continue
            if expected - relaunch_at > timedelta(minutes=5):
                continue
            pullback = later[breakout_index + 1:relaunch_index]
            stop_source = (
                min(pullback, key=lambda item: float(item["low"]))
                if direction == "BULL"
                else max(pullback, key=lambda item: float(item["high"]))
            )
            stop_source_price = float(
                stop_source["low"] if direction == "BULL" else stop_source["high"]
            )
            stop_buffer = round(touch_buffer, 1)
            stop_price = (
                stop_source_price - stop_buffer
                if direction == "BULL"
                else stop_source_price + stop_buffer
            )
            sstv = _sstv_quality_audit(
                range_bars,
                direction=direction,
                atr=atr,
                context="OPENING_RANGE",
            )
            if sstv["status"] not in {"QUALIFIED", "WATCH"}:
                continue
            side = "LONG" if direction == "BULL" else "SHORT"
            source = "OR5" if name == "or5" else "OR15"
            candidates.append(
                {
                    "setup_key": _id("SETUP", "OR_RETEST", source, side, breakout["time"]),
                    "setup_name": f"{source}突破回踩重新發動",
                    "direction": side,
                    "stage": "ARMED",
                    "trigger_operator": "CLOSE_ABOVE" if side == "LONG" else "CLOSE_BELOW",
                    "trigger_level": float(
                        max(float(item["high"]) for item in pullback)
                        if side == "LONG"
                        else min(float(item["low"]) for item in pullback)
                    ),
                    "trigger_time": relaunch["time"],
                    "first_seen_at": relaunch["time"],
                    "valid_bars": 5,
                    "stop_source_time": stop_source["time"],
                    "stop_source_price": stop_source_price,
                    "stop_buffer_points": stop_buffer,
                    "stop_price": round(stop_price, 1),
                    "stop_policy": "OR_COMPLETE_RETEST_EXTREME_PLUS_0_2_ATR_ENGINEERING_BUFFER",
                    "candidate_source": "OPENING_RANGE_BREAKOUT_RETEST",
                    "opening_range": source,
                    "opening_range_start": start_text,
                    "opening_range_end": end_text,
                    "opening_range_high": high,
                    "opening_range_low": low,
                    "breakout_time": breakout["time"],
                    "retest_time": later[retest_index]["time"],
                    "sstv_audit": sstv,
                    "decision_authority": "PROGRAM",
                    "behavior_max_wait_bars": 5,
                    "behavior_trigger_level": level,
                    "behavior_obstacles": [
                        {
                            "price": level,
                            "role": "TRIGGER",
                            "source_time": boundary_time,
                            "source_field": "high" if side == "LONG" else "low",
                        }
                    ],
                }
            )
    if not candidates:
        return None
    priority = {"OR15": 2, "OR5": 1}
    return max(
        candidates,
        key=lambda item: (
            str(item["first_seen_at"]),
            priority.get(str(item["opening_range"]), 0),
            str(item["setup_key"]),
        ),
    )


def _compression_breakout_candidate(
    anchor_state: Mapping[str, Any],
    *,
    course_method_state: Mapping[str, Any] | None,
    expected: datetime,
    bars: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any] | None:
    """Detect a 12-bar, S/S/T/V-qualified intraday compression breakout."""

    if not bars:
        return None
    records = [dict(item) for item in bars if isinstance(item, Mapping)]
    records.sort(key=lambda item: str(item.get("time") or ""))
    records = [
        item for item in records
        if item.get("time") and _aware(item["time"], "compression.bar.time") <= expected
    ]
    if len(records) < 16:
        return None
    quadrant = anchor_state.get("quadrant_context")
    quadrant = quadrant if isinstance(quadrant, Mapping) else {}
    primary = str(quadrant.get("working_primary") or "TRANSITION")
    numbered = (
        course_method_state.get("numbered_market")
        if isinstance(course_method_state, Mapping)
        else None
    )
    numbered = numbered if isinstance(numbered, Mapping) else {}
    numbered_direction = str(numbered.get("direction") or "")
    candidates: list[dict[str, Any]] = []
    first_index = max(15, len(records) - 4)
    for index in range(first_index, len(records)):
        box = records[index - 12:index]
        breakout = records[index]
        box_high = max(float(item["high"]) for item in box)
        box_low = min(float(item["low"]) for item in box)
        for direction in ("BULL", "BEAR"):
            level = box_high if direction == "BULL" else box_low
            if not _directional_breakout_bar(breakout, direction=direction, level=level):
                continue
            later = records[index + 1:]
            if any(
                (direction == "BULL" and float(item["close"]) <= level)
                or (direction == "BEAR" and float(item["close"]) >= level)
                for item in later
            ):
                continue
            breakout_at = _aware(breakout["time"], "compression.breakout.time")
            if expected - breakout_at > timedelta(minutes=3):
                continue
            atr = _atr_from_bar_records(records[:index], 14)
            if atr is None:
                continue
            audit = _sstv_quality_audit(
                box,
                direction=direction,
                atr=atr,
                context="INTRADAY_COMPRESSION",
            )
            # T is a course/context question, not merely the slope inside the
            # box. Q3 permits either edge; Q1/Q4 only permit the numbered trend.
            context_trend_support = bool(
                primary == "Q3"
                or (
                    primary in {"Q1", "Q4"}
                    and numbered_direction == direction
                )
            )
            audit = dict(audit)
            audit["checks"] = dict(audit["checks"])
            audit["checks"]["trend"] = context_trend_support
            volatility_contracting = bool(audit["checks"].get("volatility"))
            ma21_turn = _ma21_flat_then_turn(
                records,
                breakout_index=index,
                direction=direction,
                atr=float(atr),
            )
            audit["compression_prerequisites"] = {
                "range_contracting": volatility_contracting,
                "ma21_flat_then_turn": ma21_turn,
            }
            hard_reject = list(audit["hard_reject_reasons"])
            if not volatility_contracting:
                hard_reject.append("COMPRESSION_RANGE_NOT_CONTRACTING")
            if not ma21_turn:
                hard_reject.append("COMPRESSION_MA21_NOT_FLAT_THEN_TURNING")
            audit["hard_reject_reasons"] = hard_reject
            audit["support_count"] = sum(bool(value) for value in audit["checks"].values())
            audit["status"] = (
                "QUALIFIED"
                if audit["support_count"] >= 3 and not audit["hard_reject_reasons"]
                else "WATCH"
                if audit["support_count"] == 2 and not audit["hard_reject_reasons"]
                else "REJECTED"
            )
            if audit["status"] != "QUALIFIED":
                continue
            side = "LONG" if direction == "BULL" else "SHORT"
            stop_buffer = round(max(1.0, float(atr) * 0.2), 1)
            stop_source = (
                min(box, key=lambda item: float(item["low"]))
                if side == "LONG"
                else max(box, key=lambda item: float(item["high"]))
            )
            stop_source_price = float(
                stop_source["low"] if side == "LONG" else stop_source["high"]
            )
            trigger_source = (
                max(box, key=lambda item: float(item["high"]))
                if side == "LONG"
                else min(box, key=lambda item: float(item["low"]))
            )
            stop_price = (
                stop_source_price - stop_buffer
                if side == "LONG"
                else stop_source_price + stop_buffer
            )
            candidates.append(
                {
                    "setup_key": _id(
                        "SETUP", "COMPRESSION_SSTV", side,
                        box[0]["time"], box[-1]["time"],
                    ),
                    "setup_name": "多方盤中壓縮突破" if side == "LONG" else "空方盤中壓縮突破",
                    "direction": side,
                    "stage": "ARMED",
                    "trigger_operator": "CLOSE_ABOVE" if side == "LONG" else "CLOSE_BELOW",
                    "trigger_level": level,
                    "trigger_time": breakout["time"],
                    "first_seen_at": breakout["time"],
                    "valid_bars": 3,
                    "stop_source_time": stop_source["time"],
                    "stop_source_price": stop_source_price,
                    "stop_buffer_points": stop_buffer,
                    "stop_price": round(stop_price, 1),
                    "stop_policy": "COMPRESSION_OPPOSITE_EDGE_PLUS_0_2_ATR_ENGINEERING_BUFFER",
                    "candidate_source": "COMPRESSION_BREAKOUT_SSTV",
                    "compression_start": box[0]["time"],
                    "compression_end": box[-1]["time"],
                    "compression_high": box_high,
                    "compression_low": box_low,
                    "sstv_audit": audit,
                    "decision_authority": "PROGRAM",
                    "behavior_max_wait_bars": 3,
                    "behavior_trigger_level": level,
                    "behavior_obstacles": [
                        {
                            "price": level,
                            "role": "TRIGGER",
                            "source_time": trigger_source["time"],
                            "source_field": "high" if side == "LONG" else "low",
                        }
                    ],
                }
            )
    if not candidates:
        return None
    return max(candidates, key=lambda item: (str(item["first_seen_at"]), str(item["setup_key"])))


def _ma21_flat_then_turn(
    bars: Sequence[Mapping[str, Any]],
    *,
    breakout_index: int,
    direction: str,
    atr: float,
) -> bool:
    """OHLC proxy for a flat 21MA that starts turning on the breakout bar."""

    if breakout_index < 24 or atr <= 0:
        return False

    def sma_at(index: int) -> float:
        return sum(float(item["close"]) for item in bars[index - 20:index + 1]) / 21.0

    values = [sma_at(index) for index in range(breakout_index - 4, breakout_index + 1)]
    prior_slope = (values[-2] - values[0]) / 3.0
    breakout_step = values[-1] - values[-2]
    was_flat = abs(prior_slope) <= atr * 0.12
    turns = breakout_step > 0 if direction == "BULL" else breakout_step < 0
    return bool(was_flat and turns)


def _directional_breakout_bar(
    bar: Mapping[str, Any],
    *,
    direction: str,
    level: float,
) -> bool:
    opened = float(bar["open"])
    high = float(bar["high"])
    low = float(bar["low"])
    close = float(bar["close"])
    span = high - low
    if span <= 0:
        return False
    body_ratio = abs(close - opened) / span
    close_location = (close - low) / span
    if direction == "BULL":
        return bool(close > level and close > opened and body_ratio >= 0.5 and close_location >= 0.7)
    if direction == "BEAR":
        return bool(close < level and close < opened and body_ratio >= 0.5 and close_location <= 0.3)
    return False


def _sstv_quality_audit(
    bars: Sequence[Mapping[str, Any]],
    *,
    direction: str,
    atr: float | None,
    context: str,
) -> dict[str, Any]:
    """Return transparent OHLC proxies for the course S/S/T/V checklist."""

    records = [dict(item) for item in bars if isinstance(item, Mapping)]
    if len(records) < 5:
        return {
            "version": 1,
            "authority": "PROGRAM_SSTV_OHLC_PROXY_V1",
            "context": context,
            "status": "REJECTED",
            "checks": {"size": False, "stability": False, "trend": False, "volatility": False},
            "support_count": 0,
            "hard_reject_reasons": ["FEWER_THAN_FIVE_CLOSED_BARS"],
        }
    ranges = [max(0.0, float(item["high"]) - float(item["low"])) for item in records]
    bodies = [abs(float(item["close"]) - float(item["open"])) for item in records]
    median_range = _median(ranges)
    width = max(float(item["high"]) for item in records) - min(float(item["low"]) for item in records)
    effective_atr = float(atr) if isinstance(atr, (int, float)) and not isinstance(atr, bool) and atr > 0 else median_range
    size_ok = bool(width > 0 and effective_atr > 0 and width <= effective_atr * 6.0)

    high = max(float(item["high"]) for item in records)
    low = min(float(item["low"]) for item in records)
    edge_band = width * 0.2
    top_touches = sum(float(item["high"]) >= high - edge_band for item in records)
    bottom_touches = sum(float(item["low"]) <= low + edge_band for item in records)
    wick_ratios = [
        (span - body) / span if span > 0 else 1.0
        for span, body in zip(ranges, bodies)
    ]
    stability_ok = bool(top_touches >= 2 and bottom_touches >= 2 and _median(wick_ratios) <= 0.75)

    net = float(records[-1]["close"]) - float(records[0]["open"])
    trend_ok = net > 0 if direction == "BULL" else net < 0
    midpoint = max(1, len(ranges) // 2)
    early = _median(ranges[:midpoint])
    late = _median(ranges[midpoint:])
    max_range = max(ranges)
    volatility_ok = bool(
        median_range > 0
        and late <= early
        and max_range <= median_range * 2.5
    )
    hard: list[str] = []
    if width <= 0:
        hard.append("ZERO_WIDTH")
    if median_range <= 0:
        hard.append("NO_USABLE_RANGE")
    if median_range > 0 and max_range > median_range * 3.5:
        hard.append("UNCONTROLLED_BAR_SIZE")
    checks = {
        "size": size_ok,
        "stability": stability_ok,
        "trend": trend_ok,
        "volatility": volatility_ok,
    }
    support_count = sum(bool(value) for value in checks.values())
    status = (
        "QUALIFIED"
        if support_count >= 3 and not hard
        else "WATCH"
        if support_count == 2 and not hard
        else "REJECTED"
    )
    return {
        "version": 1,
        "authority": "PROGRAM_SSTV_OHLC_PROXY_V1",
        "context": context,
        "status": status,
        "checks": checks,
        "support_count": support_count,
        "hard_reject_reasons": hard,
        "box_width": round(width, 4),
        "atr_reference": round(effective_atr, 4),
        "median_bar_range": round(median_range, 4),
        "top_touch_count": top_touches,
        "bottom_touch_count": bottom_touches,
    }


def _median(values: Sequence[float]) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _select_program_entry_candidate(
    continuation: Mapping[str, Any] | None,
    false_break: Mapping[str, Any] | None,
    momentum: Mapping[str, Any] | None = None,
    q2_reversal: Mapping[str, Any] | None = None,
    opening_range: Mapping[str, Any] | None = None,
    compression: Mapping[str, Any] | None = None,
    *,
    anchor_state: Mapping[str, Any] | None = None,
    allowed_directions: set[str] | None = None,
    allowed_sources: set[str] | None = None,
) -> dict[str, Any] | None:
    """Choose one executable plan by causal recency and fixed tie priority."""

    allowed = allowed_directions or {"LONG", "SHORT"}
    candidates = [
        dict(item)
        for item in (
            continuation,
            false_break,
            momentum,
            q2_reversal,
            opening_range,
            compression,
        )
        if isinstance(item, Mapping) and item.get("setup_key")
        and item.get("direction") in allowed
        and (allowed_sources is None or item.get("candidate_source") in allowed_sources)
        and not _candidate_is_countertrend_to_intact_controller(item, anchor_state)
    ]
    if not candidates:
        return None
    priority = {
        "YIZHI_EARLY_BREAKOUT": 5,
        "OPENING_RANGE_BREAKOUT_RETEST": 4,
        "COMPRESSION_BREAKOUT_SSTV": 3,
        "Q2_FAILED_REVERSE_CANDIDATE": 2,
        "FALSE_BREAK_RECLAIM": 2,
        "CONFIRMED_PULLBACK_ENDPOINT_N2": 1,
        "ANCHOR_LEG_SEQUENCE": 1,
    }
    return max(
        candidates,
        key=lambda item: (
            str(item.get("first_seen_at") or ""),
            priority.get(str(item.get("candidate_source") or ""), 0),
            str(item.get("setup_key") or ""),
        ),
    )


def _retained_course_controller(
    anchor_state: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Return the highest anchor whose course background has not been replaced.

    A broken defense degrades an anchor; it does not erase the dynasty.  In
    particular, ``DEGRADED_RECLAIMED`` is still the original background while
    the market waits for continuation or an opposite child takeover.  Treating
    that state as "no controller" allowed unrelated old defenses to create
    countertrend orders.
    """

    if not isinstance(anchor_state, Mapping):
        return None
    retained = {None, "ACTIVE", "DEGRADED", "DEGRADED_RECLAIMED"}
    background = anchor_state.get("background_anchor")
    child = anchor_state.get("child_anchor")
    if isinstance(background, Mapping) and background.get("status") in retained:
        return background
    if isinstance(child, Mapping) and child.get("status") in retained:
        return child
    return None


def _active_opposite_child_confirms_direction(
    anchor_state: Mapping[str, Any] | None,
    direction: str | None,
) -> bool:
    """Recognize the program's right-side takeover without model judgment."""

    if not isinstance(anchor_state, Mapping) or direction not in {"BULL", "BEAR"}:
        return False
    background = anchor_state.get("background_anchor")
    child = anchor_state.get("child_anchor")
    return bool(
        isinstance(background, Mapping)
        and background.get("direction") in {"BULL", "BEAR"}
        and background.get("direction") != direction
        and isinstance(child, Mapping)
        and child.get("status") == "ACTIVE"
        and child.get("direction") == direction
    )


def _candidate_course_direction(candidate: Mapping[str, Any]) -> str | None:
    return {"LONG": "BULL", "SHORT": "BEAR"}.get(
        str(candidate.get("direction") or "")
    )


def _candidate_is_countertrend_to_intact_controller(
    candidate: Mapping[str, Any],
    anchor_state: Mapping[str, Any] | None,
) -> bool:
    """Keep one-contract Type1/left-side ideas observational until takeover.

    A false break remains in ``false_break_arm_candidate`` for the method scan,
    but it cannot become the selected executable setup while the highest
    active anchor still points the other way.  An anchor that has not yet
    earned a Dow defense is still an active background; absence of a defense
    is not evidence of invalidation.  A recorded broken defense releases this
    guard while the lifecycle completes its downgrade/takeover transition.
    """

    if not isinstance(anchor_state, Mapping):
        return False
    controller = _retained_course_controller(anchor_state)
    if not isinstance(controller, Mapping):
        return False
    candidate_direction = _candidate_course_direction(candidate)
    if _active_opposite_child_confirms_direction(anchor_state, candidate_direction):
        return False
    return bool(
        candidate_direction is not None
        and controller.get("direction") != candidate_direction
    )


def _current_course_defense_ids(
    anchor_state: Mapping[str, Any],
    *,
    direction: str,
) -> set[str]:
    context = anchor_state.get("dow_context")
    if not isinstance(context, Mapping):
        return set()
    side = "bull" if direction == "BULL" else "bear"
    result: set[str] = set()
    for grade in ("large", "small"):
        defense = context.get(f"{grade}_{side}_defense")
        if isinstance(defense, Mapping) and defense.get("id"):
            result.add(str(defense["id"]))
    return result


def _apply_program_course_entry_quality_gate(
    candidate: Mapping[str, Any] | None,
    *,
    anchor_state: Mapping[str, Any],
    course_method_state: Mapping[str, Any],
    structure_events: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Classify a structural candidate as executable or observation-only.

    Detecting a pullback endpoint or reclaim is necessary evidence, not by
    itself a course-qualified trade.  This gate fixes the missing layer that
    previously let every mechanical n=2 turn become an order.  It contains no
    fitted point threshold and uses only state already known at ``as_of``.
    """

    audit: dict[str, Any] = {
        "version": 1,
        "authority": "PROGRAM_COURSE_GATE_V1",
        "status": "NO_CANDIDATE",
        "setup_key": None,
        "candidate_source": None,
        "reason_codes": ["NO_STRUCTURAL_CANDIDATE"],
    }
    if not isinstance(candidate, Mapping):
        return None, audit

    selected = dict(candidate)
    source = str(selected.get("candidate_source") or "")
    audit.update(
        {
            "status": "OBSERVATION_ONLY",
            "setup_key": str(selected.get("setup_key") or "") or None,
            "candidate_source": source or None,
            "reason_codes": [],
        }
    )
    reasons: list[str] = []

    if _candidate_is_countertrend_to_intact_controller(selected, anchor_state):
        reasons.append("COUNTERTREND_TO_HIGHEST_ACTIVE_ANCHOR")

    quadrant = anchor_state.get("quadrant_context")
    quadrant = quadrant if isinstance(quadrant, Mapping) else {}
    primary = str(quadrant.get("working_primary") or "UNDEFINED")
    candidates = {
        str(item)
        for item in quadrant.get("working_candidates", [])
        if item in {"Q1", "Q2", "Q3", "Q4"}
    }
    trend = str(quadrant.get("working_trend_dynamics") or "UNCLEAR")
    taiji = anchor_state.get("taiji_context")
    taiji = taiji if isinstance(taiji, Mapping) else {}
    taiji_state = str(taiji.get("program_state") or "UNDEFINED")
    cclass = str(course_method_state.get("cclass_mode") or "UNDEFINED")
    numbered = course_method_state.get("numbered_market")
    numbered = numbered if isinstance(numbered, Mapping) else {}
    candidate_direction = _candidate_course_direction(selected)
    numbered_direction = str(numbered.get("direction") or "")
    numbered_value = numbered.get("number")
    numbered_market_risk_flags: list[str] = []
    if isinstance(numbered_value, int) and numbered_value >= 2:
        # The course describes a 2+ numbered market as noisier and less suited
        # to repeatedly flipping direction.  It does not restrict every later
        # trade to an outer false break.  Preserve that fact as an explicit AI
        # quality-review warning while leaving objective strategy gates below
        # responsible for executable versus observation-only classification.
        numbered_market_risk_flags.append(
            "NUMBERED_MARKET_2_PLUS_HIGH_NOISE_AI_REVIEW"
        )
    audit["numbered_market"] = {
        "status": numbered.get("status"),
        "number": numbered_value,
        "direction": numbered_direction or None,
        "restriction": numbered.get("restriction"),
        "risk_flags": list(numbered_market_risk_flags),
        "requires_ai_quality_review": bool(numbered_market_risk_flags),
    }
    audit["risk_flags"] = list(numbered_market_risk_flags)
    audit["requires_ai_quality_review"] = bool(numbered_market_risk_flags)
    if numbered.get("status") != "NUMBERED":
        reasons.append("NUMBERED_MARKET_UNDEFINED")
    elif candidate_direction != numbered_direction:
        reasons.append("NUMBERED_MARKET_DIRECTION_MISMATCH")
    if source in {"ANCHOR_LEG_SEQUENCE", "CONFIRMED_PULLBACK_ENDPOINT_N2"}:
        if cclass in {"RESETTING", "UNDEFINED"}:
            reasons.append(f"CONTINUATION_CCLASS_{cclass}")
        if taiji_state in {"COPY_FAILED", "CORRECTION_DESTRUCTIVE", "UNDEFINED"}:
            reasons.append(f"CONTINUATION_TAIJI_{taiji_state}")
        # This candidate owns a complete pullback endpoint and therefore is
        # specifically a Q4 low-buy/high-sell plan.  Q1/Yizhi entries require
        # their own early-breakout, tight time/momentum invalidation contract;
        # accepting an already extended Q1 push here would enter late while
        # incorrectly using the much wider Q4 correction stop.
        quadrant_supports_continuation = (
            primary == "Q4"
            or (
                primary == "TRANSITION"
                and "Q4" in candidates
                and trend == "INCREASING"
            )
        )
        if not quadrant_supports_continuation:
            reasons.append(f"CONTINUATION_WORKING_QUADRANT_{primary}_{trend}")
    elif source == "Q2_FAILED_REVERSE_CANDIDATE":
        candidate_direction = _candidate_course_direction(selected)
        controller = _retained_course_controller(anchor_state)
        controller_direction = (
            str(controller.get("direction"))
            if isinstance(controller, Mapping)
            and controller.get("direction") in {"BULL", "BEAR"}
            else None
        )
        defense = controller.get("defense") if isinstance(controller, Mapping) else None
        reverse = anchor_state.get("reverse_candidate")
        working = anchor_state.get("working_leg")
        opposite = "BEAR" if candidate_direction == "BULL" else "BULL"
        endpoint_price = selected.get("stop_source_price")
        reverse_price = reverse.get("current_extreme_price") if isinstance(reverse, Mapping) else None
        if controller_direction != candidate_direction:
            reasons.append("Q2_REVERSAL_NOT_ALIGNED_WITH_RETAINED_CONTROLLER")
        if not (
            isinstance(defense, Mapping)
            and defense.get("id")
            and defense.get("state") in {None, "ACTIVE"}
        ):
            reasons.append("Q2_REVERSAL_CONTROLLER_DEFENSE_NOT_ACTIVE")
        if not (
            isinstance(reverse, Mapping)
            and reverse.get("direction") == opposite
            and reverse.get("status") in {"QUALIFIED", "AWAITING_CONTINUATION"}
        ):
            reasons.append("Q2_REVERSAL_OPPOSITE_CANDIDATE_NOT_MATURE")
        if not isinstance(working, Mapping) or working.get("direction") != opposite:
            reasons.append("Q2_REVERSAL_NOT_AT_COUNTERMOVE_ENDPOINT")
        if not (
            primary == "Q2"
            or (primary == "TRANSITION" and "Q2" in candidates)
        ):
            reasons.append(f"Q2_REVERSAL_WORKING_QUADRANT_{primary}_{trend}")
        if cclass != "RESETTING":
            reasons.append(f"Q2_REVERSAL_CCLASS_{cclass}")
        if taiji_state not in {"CORRECTION_DESTRUCTIVE", "COPY_FAILED"}:
            reasons.append(f"Q2_REVERSAL_TAIJI_{taiji_state}")
        if not (
            isinstance(endpoint_price, (int, float))
            and not isinstance(endpoint_price, bool)
            and isinstance(reverse_price, (int, float))
            and not isinstance(reverse_price, bool)
            and abs(float(endpoint_price) - float(reverse_price)) <= 1e-9
        ):
            reasons.append("Q2_REVERSAL_ENDPOINT_NOT_REVERSE_EXTREME")
    elif source == "YIZHI_EARLY_BREAKOUT":
        yizhi = course_method_state.get("yizhi")
        yizhi = yizhi if isinstance(yizhi, Mapping) else {}
        candidate_direction = _candidate_course_direction(selected)
        if cclass != "YIZHI_MOMENTUM":
            reasons.append(f"YIZHI_CCLASS_{cclass}")
        if yizhi.get("state") not in {
            "CENTRIFUGAL_CONFIRMED",
            "GOLD_DRAGON",
            "K_GOLD_DRAGON",
            "MOMENTUM_CONTINUING",
        }:
            reasons.append(f"YIZHI_STATE_{yizhi.get('state') or 'UNDEFINED'}")
        if yizhi.get("direction") != candidate_direction:
            reasons.append("YIZHI_DIRECTION_MISMATCH")
        if not (
            primary == "Q1"
            or (
                primary == "TRANSITION"
                and "Q1" in candidates
                and trend == "INCREASING"
            )
        ):
            reasons.append(f"YIZHI_WORKING_QUADRANT_{primary}_{trend}")
    elif source == "OPENING_RANGE_BREAKOUT_RETEST":
        sstv = selected.get("sstv_audit")
        sstv = sstv if isinstance(sstv, Mapping) else {}
        audit["sstv"] = dict(sstv)
        if selected.get("opening_range") not in {"OR5", "OR15"}:
            reasons.append("OR_SOURCE_RANGE_INVALID")
        if not selected.get("breakout_time") or not selected.get("retest_time"):
            reasons.append("OR_BREAKOUT_RETEST_SEQUENCE_INCOMPLETE")
        if sstv.get("status") not in {"QUALIFIED", "WATCH"}:
            reasons.append(f"OR_SSTV_{sstv.get('status') or 'MISSING'}")
        if primary == "Q2":
            reasons.append("OR_BREAKOUT_FORBIDDEN_IN_Q2")
        elif primary == "Q3" and sstv.get("status") != "QUALIFIED":
            reasons.append("OR_Q3_REQUIRES_QUALIFIED_SSTV")
        elif primary not in {"Q1", "Q3", "Q4"}:
            reasons.append(f"OR_WORKING_QUADRANT_{primary}")
    elif source == "COMPRESSION_BREAKOUT_SSTV":
        sstv = selected.get("sstv_audit")
        sstv = sstv if isinstance(sstv, Mapping) else {}
        audit["sstv"] = dict(sstv)
        if sstv.get("status") != "QUALIFIED":
            reasons.append(f"COMPRESSION_SSTV_{sstv.get('status') or 'MISSING'}")
        if primary == "Q2":
            reasons.append("COMPRESSION_FORBIDDEN_IN_Q2")
        elif primary not in {"Q1", "Q3", "Q4"}:
            reasons.append(f"COMPRESSION_WORKING_QUADRANT_{primary}")
        if primary in {"Q1", "Q4"} and candidate_direction != numbered_direction:
            reasons.append("COMPRESSION_TREND_DIRECTION_MISMATCH")
    elif source == "FALSE_BREAK_RECLAIM":
        source_event = next(
            (
                item
                for item in structure_events
                if isinstance(item, Mapping)
                and item.get("id") == selected.get("source_event_id")
            ),
            None,
        )
        boundary_type = str(
            selected.get("source_type")
            or (source_event.get("source_type") if isinstance(source_event, Mapping) else "")
        )
        audit["boundary_type"] = boundary_type or None
        candidate_direction = _candidate_course_direction(selected)
        controller = _retained_course_controller(anchor_state)
        controller_direction = (
            str(controller.get("direction"))
            if isinstance(controller, Mapping)
            and controller.get("direction") in {"BULL", "BEAR"}
            else None
        )
        aligned_with_controller = bool(
            candidate_direction is not None
            and (
                controller_direction == candidate_direction
                or _active_opposite_child_confirms_direction(
                    anchor_state,
                    candidate_direction,
                )
            )
        )
        audit["course_direction"] = candidate_direction
        audit["controller_direction"] = controller_direction
        if boundary_type not in {
            "DOW_DEFENSE",
            "OR5_LOW", "OR5_HIGH",
            "OR15_LOW", "OR15_HIGH",
        }:
            reasons.append(f"FALSE_BREAK_BOUNDARY_NOT_QUALIFIED_{boundary_type or 'UNKNOWN'}")
        elif not aligned_with_controller:
            reasons.append("FALSE_BREAK_NOT_ALIGNED_WITH_RETAINED_COURSE_CONTROLLER")
        elif boundary_type == "DOW_DEFENSE":
            current_defenses = _current_course_defense_ids(
                anchor_state,
                direction=str(candidate_direction),
            )
            source_id = str(
                selected.get("source_id")
                or (source_event.get("source_id") if isinstance(source_event, Mapping) else "")
            )
            audit["current_course_defense"] = source_id in current_defenses
            if source_id not in current_defenses:
                reasons.append("FALSE_BREAK_DOW_DEFENSE_NOT_CURRENT_COURSE_SLOT")
        else:
            # OR5/OR15 is an opening checkpoint, not automatic proof of a
            # second leg.  It becomes executable only after the same-direction
            # controller owns a formal defense; otherwise X-process treats the
            # first endpoint break/reclaim as observation.
            controller_defense = (
                controller.get("defense") if isinstance(controller, Mapping) else None
            )
            if not (
                isinstance(controller_defense, Mapping)
                and controller_defense.get("id")
            ):
                reasons.append("FALSE_BREAK_OPENING_BOUNDARY_FIRST_ENDPOINT_ONLY")
    else:
        reasons.append(f"UNSUPPORTED_CANDIDATE_SOURCE_{source or 'UNKNOWN'}")

    if reasons:
        audit["reason_codes"] = reasons
        return None, audit
    audit["status"] = "EXECUTABLE"
    audit["reason_codes"] = ["ALL_PROGRAM_COURSE_ENTRY_GATES_PASSED"]
    selected["course_entry_quality"] = "EXECUTABLE"
    selected["course_entry_quality_authority"] = "PROGRAM_COURSE_GATE_V1"
    return selected, audit


_AI_HYBRID_Q2_Q4_SOURCES = {
    "ANCHOR_LEG_SEQUENCE",
    "CONFIRMED_PULLBACK_ENDPOINT_N2",
    "FALSE_BREAK_RECLAIM",
    "Q2_FAILED_REVERSE_CANDIDATE",
    "Q2_SLOW_OUTER_EXPANSION_FAILURE",
    "Q4_AGGRESSIVE_PULLBACK_REVERSAL",
}
_AI_HYBRID_Q2_Q4_STRATEGIES = {
    "ANCHOR_LEG_SEQUENCE": "Q4_PULLBACK_CONTINUATION",
    "CONFIRMED_PULLBACK_ENDPOINT_N2": "Q4_PULLBACK_CONTINUATION",
    "FALSE_BREAK_RECLAIM": "Q2_FALSE_BREAK_RECLAIM",
    "Q2_FAILED_REVERSE_CANDIDATE": "Q2_FAILED_COUNTERTREND_REVERSAL",
    "Q2_SLOW_OUTER_EXPANSION_FAILURE": "Q2_SLOW_OUTER_EXPANSION_FAILURE",
    "Q4_AGGRESSIVE_PULLBACK_REVERSAL": "Q4_AGGRESSIVE_PULLBACK_REVERSAL",
}
_AI_HYBRID_INTERPRETIVE_REASON_PREFIXES = (
    "COUNTERTREND_TO_HIGHEST_ACTIVE_ANCHOR",
    "FALSE_BREAK_NOT_ALIGNED_WITH_RETAINED_COURSE_CONTROLLER",
    "NUMBERED_MARKET_",
    "CONTINUATION_CCLASS_",
    "CONTINUATION_TAIJI_",
    "CONTINUATION_WORKING_QUADRANT_",
    "Q2_REVERSAL_WORKING_QUADRANT_",
    "Q2_REVERSAL_CCLASS_",
    "Q2_REVERSAL_TAIJI_",
)


def _build_ai_hybrid_q2_q4_candidate_inventory(
    candidates: Sequence[Mapping[str, Any] | None],
    *,
    expected: datetime,
    anchor_state: Mapping[str, Any],
    course_method_state: Mapping[str, Any],
    structure_events: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Publish every hard-valid long Q2/Q4 option for independent AI review.

    The legacy course gate mixes objective setup validity with program-owned
    quadrant, Taiji, C-class and numbered-market interpretations.  PROGRAM_ONLY
    continues to use that gate unchanged.  AI_HYBRID instead retains every
    candidate that passes objective/policy checks and records the interpretive
    disagreements as an audit that is not serialized to the model.
    """

    inventory: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in candidates:
        if not isinstance(raw, Mapping):
            continue
        candidate = dict(raw)
        setup_key = str(candidate.get("setup_key") or "")
        source = str(candidate.get("candidate_source") or "")
        hard_reasons = _ai_hybrid_candidate_basic_hard_reasons(
            candidate,
            expected=expected,
        )
        if source in {
            "Q2_SLOW_OUTER_EXPANSION_FAILURE",
            "Q4_AGGRESSIVE_PULLBACK_REVERSAL",
        }:
            # These builders deliberately publish hard facts only. Their Q2/Q4,
            # Taiji and location-quality contract belongs to the AI semantic
            # gate, so the legacy program interpretation is not an applicable
            # prefilter. PROGRAM_ONLY never calls these builders.
            legacy_audit = {
                "status": "NOT_APPLICABLE_AI_INTERPRETATION_REQUIRED",
                "reason_codes": [],
            }
        else:
            _, legacy_audit = _apply_program_course_entry_quality_gate(
                candidate,
                anchor_state=anchor_state,
                course_method_state=course_method_state,
                structure_events=structure_events,
            )
        legacy_reasons = [
            str(item)
            for item in legacy_audit.get("reason_codes", [])
            if item and item != "ALL_PROGRAM_COURSE_ENTRY_GATES_PASSED"
        ]
        interpretive_reasons = [
            code for code in legacy_reasons
            if code.startswith(_AI_HYBRID_INTERPRETIVE_REASON_PREFIXES)
        ]
        hard_reasons.extend(
            code for code in legacy_reasons
            if not code.startswith(_AI_HYBRID_INTERPRETIVE_REASON_PREFIXES)
        )
        hard_reasons = list(dict.fromkeys(hard_reasons))
        audit = {
            "version": 1,
            "authority": "PROGRAM_HARD_FACT_GATE_V1",
            "setup_key": setup_key or None,
            "candidate_source": source or None,
            "hard_gate_status": "REJECTED" if hard_reasons else "PASSED",
            "hard_reason_codes": hard_reasons,
            "interpretive_status": "CONFLICTING" if interpretive_reasons else "SUPPORTED",
            "interpretive_reason_codes": interpretive_reasons,
            "interpretive_authority": "PROGRAM_ADVISORY_ONLY_V1",
            "legacy_course_gate_status": legacy_audit.get("status"),
        }
        audits.append(audit)
        if hard_reasons or not setup_key or setup_key in seen:
            continue
        selected = dict(candidate)
        selected["decision_authority"] = "AI_HYBRID"
        selected["hard_fact_gate"] = {
            "status": "PASSED",
            "authority": "PROGRAM_HARD_FACT_GATE_V1",
        }
        inventory.append(selected)
        seen.add(setup_key)
    return inventory, audits


def _ai_hybrid_candidate_basic_hard_reasons(
    candidate: Mapping[str, Any],
    *,
    expected: datetime,
) -> list[str]:
    reasons: list[str] = []
    setup_key = candidate.get("setup_key")
    source = str(candidate.get("candidate_source") or "")
    if not isinstance(setup_key, str) or not setup_key.strip():
        reasons.append("SETUP_KEY_MISSING")
    if candidate.get("direction") != "LONG":
        reasons.append("LONG_ONLY_DIRECTION_REQUIRED")
    if source not in _AI_HYBRID_Q2_Q4_SOURCES:
        reasons.append("Q2_Q4_SOURCE_REQUIRED")
    elif candidate.get("entry_strategy") != _AI_HYBRID_Q2_Q4_STRATEGIES[source]:
        reasons.append("Q2_Q4_ENTRY_STRATEGY_MISMATCH")
    if candidate.get("trigger_operator") not in {"CLOSE_ABOVE", "RECLAIM_ABOVE"}:
        reasons.append("LONG_TRIGGER_OPERATOR_INVALID")
    trigger = candidate.get("trigger_level")
    stop = candidate.get("stop_price")
    stop_source = candidate.get("stop_source_price")
    if not _is_positive_number(trigger):
        reasons.append("TRIGGER_LEVEL_INVALID")
    if not _is_positive_number(stop):
        reasons.append("STOP_PRICE_INVALID")
    if not _is_positive_number(stop_source):
        reasons.append("STOP_SOURCE_PRICE_INVALID")
    if _is_positive_number(trigger) and _is_positive_number(stop) and float(stop) >= float(trigger):
        reasons.append("LONG_STOP_NOT_BELOW_TRIGGER")
    valid_bars = candidate.get("valid_bars")
    if isinstance(valid_bars, bool) or not isinstance(valid_bars, int) or valid_bars <= 0:
        reasons.append("VALID_BARS_INVALID")
    for field in ("first_seen_at", "trigger_time", "stop_source_time"):
        raw_time = candidate.get(field)
        if not isinstance(raw_time, str) or not raw_time:
            reasons.append(f"{field.upper()}_MISSING")
            continue
        try:
            known = _aware(raw_time, field)
        except (DeterministicReplayError, TypeError, ValueError):
            reasons.append(f"{field.upper()}_INVALID")
            continue
        if known > expected:
            reasons.append(f"{field.upper()}_FROM_FUTURE")
    if source == "FALSE_BREAK_RECLAIM" and not candidate.get("source_event_id"):
        reasons.append("FALSE_BREAK_SOURCE_EVENT_MISSING")
    if source == "Q2_FAILED_REVERSE_CANDIDATE":
        for field in (
            "q2_reverse_candidate_ref",
            "q2_controller_ref",
            "q2_controller_defense_ref",
        ):
            if not candidate.get(field):
                reasons.append(f"{field.upper()}_MISSING")
    if source in {
        "FALSE_BREAK_RECLAIM",
        "Q2_SLOW_OUTER_EXPANSION_FAILURE",
        "Q4_AGGRESSIVE_PULLBACK_REVERSAL",
    }:
        facts_hash = candidate.get("facts_hash")
        if not isinstance(facts_hash, str) or not facts_hash.startswith("FACTS-"):
            reasons.append("FACTS_HASH_MISSING")
        facts_cutoff = candidate.get("facts_cutoff")
        if not isinstance(facts_cutoff, str):
            reasons.append("FACTS_CUTOFF_MISSING")
        else:
            try:
                if _aware(facts_cutoff, "facts_cutoff") > expected:
                    reasons.append("FACTS_CUTOFF_FROM_FUTURE")
            except (DeterministicReplayError, TypeError, ValueError):
                reasons.append("FACTS_CUTOFF_INVALID")
        if source in {
            "Q2_SLOW_OUTER_EXPANSION_FAILURE",
            "Q4_AGGRESSIVE_PULLBACK_REVERSAL",
        }:
            validity = candidate.get("validity_policy")
            if not isinstance(validity, Mapping) or validity.get("kind") != "STRUCTURAL":
                reasons.append("STRUCTURAL_VALIDITY_POLICY_REQUIRED")
    if source == "Q2_SLOW_OUTER_EXPANSION_FAILURE":
        for field in (
            "boundary_ref",
            "reverse_anchor_ref",
            "broken_defense_ref",
            "right_left_pullback_ref",
            "source_event_id",
        ):
            if not candidate.get(field):
                reasons.append(f"{field.upper()}_MISSING")
    if source == "Q4_AGGRESSIVE_PULLBACK_REVERSAL":
        for field in (
            "controller_anchor_ref",
            "controller_defense_ref",
            "parent_push_ref",
        ):
            if not candidate.get(field):
                reasons.append(f"{field.upper()}_MISSING")
        if candidate.get("confirmation_style") != "AGGRESSIVE":
            reasons.append("Q4_AGGRESSIVE_CONFIRMATION_STYLE_REQUIRED")
        if candidate.get("stop_extreme_status") != "CAUSAL_SO_FAR_NOT_N2":
            reasons.append("Q4_CAUSAL_SO_FAR_STOP_REQUIRED")
    return reasons


def _is_positive_number(value: Any) -> bool:
    return bool(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and float(value) > 0
    )


def _confirmed_pullback_endpoint_candidate(
    controller: Mapping[str, Any],
    *,
    bars: Sequence[Mapping[str, Any]] | None,
    expected: datetime,
) -> dict[str, Any] | None:
    """Build a fresh continuation setup from a causally held correction endpoint.

    This is an execution endpoint, not a new anchor or Dow pivot.  It uses the
    same two-right-bar observation delay as the course n=2 chain, but does not
    require price to first close beyond an outside candle's opposite extreme.
    """

    direction = str(controller.get("direction") or "")
    if direction not in {"BULL", "BEAR"} or not bars:
        return None
    if controller.get("status") not in {None, "ACTIVE"}:
        return None

    normalized = []
    for raw in bars:
        if not isinstance(raw, Mapping):
            continue
        at = _aware(raw.get("time"), "pullback_bar.time")
        if at > expected:
            continue
        normalized.append(
            {
                "time": at.isoformat(),
                "open": _positive_number(raw.get("open"), "pullback_bar.open"),
                "high": _positive_number(raw.get("high"), "pullback_bar.high"),
                "low": _positive_number(raw.get("low"), "pullback_bar.low"),
                "close": _positive_number(raw.get("close"), "pullback_bar.close"),
            }
        )
    normalized.sort(key=lambda item: item["time"])
    if len(normalized) < 5:
        return None

    controller_known = _aware(
        controller.get("first_seen_at") or controller.get("origin_time"),
        "controller.first_seen_at",
    )
    defense = controller.get("defense")
    defense_price = (
        float(defense.get("price"))
        if isinstance(defense, Mapping)
        and isinstance(defense.get("price"), (int, float))
        and not isinstance(defense.get("price"), bool)
        and defense.get("state") in {None, "ACTIVE"}
        else None
    )
    origin_price = _positive_number(controller.get("origin_price"), "controller.origin_price")
    endpoint_index = None
    for index in range(1, len(normalized) - 2):
        bar = normalized[index]
        at = _aware(bar["time"], "pullback_endpoint.time")
        if at <= controller_known:
            continue
        # Only one left bar is required here because this endpoint is an
        # execution pullback, not a newly established direction/anchor.  Two
        # later bars must still hold it before it becomes observable.
        left = normalized[index - 1 : index]
        right = normalized[index + 1 : index + 3]
        if direction == "BULL":
            # Equal lows are one correction zone, not proof that no endpoint
            # exists.  Accept the final bar of a held plateau while requiring
            # at least one strict comparison, so a completely flat sequence
            # cannot manufacture a pivot.
            local = all(item["low"] >= bar["low"] for item in left + right) and any(
                item["low"] > bar["low"] for item in left + right
            )
            structurally_held = bar["low"] > origin_price and (
                defense_price is None or bar["low"] > defense_price
            )
            recovered = right[-1]["close"] > bar["close"]
        else:
            local = all(item["high"] <= bar["high"] for item in left + right) and any(
                item["high"] < bar["high"] for item in left + right
            )
            structurally_held = bar["high"] < origin_price and (
                defense_price is None or bar["high"] < defense_price
            )
            recovered = right[-1]["close"] < bar["close"]
        if local and structurally_held and recovered:
            endpoint_index = index

    if endpoint_index is None:
        return None
    endpoint = normalized[endpoint_index]
    confirmation = normalized[endpoint_index + 1 : endpoint_index + 3]
    prior = normalized[endpoint_index - 1]
    if direction == "BULL":
        trigger_bar = max(confirmation, key=lambda item: (item["high"], item["time"]))
        trigger_price = trigger_bar["high"]
        correction_start_price = prior["high"]
        side = "LONG"
        operator = "CLOSE_ABOVE"
    else:
        trigger_bar = min(confirmation, key=lambda item: (item["low"], item["time"]))
        trigger_price = trigger_bar["low"]
        correction_start_price = prior["low"]
        side = "SHORT"
        operator = "CLOSE_BELOW"

    return _attach_program_continuation_stop({
        "setup_key": _id(
            "SETUP",
            "PULLBACK_ENDPOINT_CONTINUATION",
            str(controller.get("id") or "CONTROLLER"),
            side,
            endpoint["time"],
            trigger_bar["time"],
        ),
        "setup_name": "多方修正後複製" if direction == "BULL" else "空方修正後複製",
        "direction": side,
        "stage": "ARMED",
        "trigger_operator": operator,
        "trigger_level": float(trigger_price),
        "trigger_time": trigger_bar["time"],
        "stop_source_time": endpoint["time"],
        "stop_source_price": float(endpoint["low"] if direction == "BULL" else endpoint["high"]),
        "correction_start_time": prior["time"],
        "correction_start_price": float(correction_start_price),
        "first_seen_at": confirmation[-1]["time"],
        "valid_bars": 5,
        "authority": "PROGRAM_CANDIDATE_NOT_AUTOMATIC_ENTRY",
        "candidate_source": "CONFIRMED_PULLBACK_ENDPOINT_N2",
        "entry_strategy": "Q4_PULLBACK_CONTINUATION",
    }, bars=normalized)


def _course_dow_label(state: Any) -> str:
    if state in {"BULL", "BEAR", "UNDEFINED"}:
        return str(state)
    if state in {"CONFLICT", "BULL_WITH_BEAR_REVERSAL", "BEAR_WITH_BULL_REVERSAL"}:
        return "TRANSITION"
    return "UNDEFINED"


def evidence_events(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return newly observable objective events using their true causal time."""

    old_pivots = _ids(previous, "pivots")
    old_legs = _ids(previous, "legs")
    old_defenses = _ids(previous, "defenses")
    old_structure_events = _ids(previous, "structure_events")
    old_anchor_events = _ids(previous, "anchor_events")
    events: list[dict[str, Any]] = []
    for collection, old, event_type, time_field in (
        (current.get("pivots"), old_pivots, "PIVOT_CONFIRMED", "first_seen_at"),
        (current.get("legs"), old_legs, "LEG_OBSERVED", "first_seen_at"),
        (current.get("defenses"), old_defenses, "DEFENSE_ACTIVATED", "first_seen_at"),
        (current.get("structure_events"), old_structure_events, "STRUCTURE_EVENT", "first_seen_at"),
        (current.get("anchor_events"), old_anchor_events, "ANCHOR_LIFECYCLE_EVENT", "first_seen_at"),
    ):
        for item in collection if isinstance(collection, list) else []:
            if isinstance(item, Mapping) and item.get("id") not in old:
                events.append(
                    {
                        "event_type": event_type,
                        "event_id": item.get("id"),
                        "event_time": item.get(time_field),
                        "recorded_at": current.get("as_of"),
                    }
                )
    previous_defenses = _items_by_id(previous, "defenses")
    for item in current.get("defenses") if isinstance(current.get("defenses"), list) else []:
        if not isinstance(item, Mapping) or item.get("state") != "BROKEN":
            continue
        old = previous_defenses.get(str(item.get("id")))
        if not isinstance(old, Mapping) or old.get("state") != "BROKEN":
            events.append(
                {
                    "event_type": "DEFENSE_BROKEN",
                    "event_id": item.get("id"),
                    "event_time": item.get("broken_at"),
                    "recorded_at": current.get("as_of"),
                }
            )
    return sorted(events, key=lambda item: (str(item.get("event_time")), str(item.get("event_id"))))


def build_deterministic_timeline(
    bars: Sequence[Mapping[str, Any]],
    *,
    session_key: str,
    anchor_lifecycle_enabled: bool = False,
    course_chain_enabled: bool = False,
    decision_authority: str = "PROGRAM",
    trade_direction_policy: str = "BOTH",
    trade_setup_policy: str = "ALL",
    initial_bars: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Replay every closed bar and retain only material objective state changes.

    This is intentionally AI-free. Precomputing a historical schedule is safe
    because every event is stamped at the first cutoff where its evidence was
    causally available; an analyzer still receives bars only through that time.
    """

    normalized = _validated_contiguous_bars(bars)
    timeline: list[dict[str, Any]] = []
    history = _validated_contiguous_bars(initial_bars) if initial_bars else []
    # `initial_bars` belong to the prior monitoring segment.  They remain
    # available to build the read-only reference snapshot, but they must not
    # seed active anchors, defenses or event-consumption state in this segment.
    previous: Mapping[str, Any] | None = None
    stable_background: str | None = None
    stable_working: str | None = None
    background_candidate: str | None = None
    working_candidate: str | None = None
    background_candidate_count = 0
    working_candidate_count = 0
    background_changed_at: datetime | None = None
    working_changed_at: datetime | None = None
    previous_anchor: str | None = None
    for index in range(len(normalized)):
        session_prefix = normalized[: index + 1]
        prefix = history + session_prefix
        expected = _aware(session_prefix[-1]["time"], "bar.time")
        structured = _causal_structured_snapshot(session_prefix)
        ledger = build_evidence_ledger(
            structured,
            bars=prefix,
            expected_as_of=expected.isoformat(),
            session_key=session_key,
            previous_ledger=previous,
            anchor_lifecycle_enabled=anchor_lifecycle_enabled,
            course_chain_enabled=course_chain_enabled,
            decision_authority=decision_authority,
            trade_direction_policy=trade_direction_policy,
            trade_setup_policy=trade_setup_policy,
        )
        raw_events = evidence_events(previous, ledger)
        raw_by_id = _items_by_id(ledger, "structure_events")
        anchor_raw_by_id = _items_by_id(ledger, "anchor_events")
        material: list[dict[str, Any]] = []
        for event in raw_events:
            event_type = str(event.get("event_type") or "")
            if event_type == "STRUCTURE_EVENT":
                raw = raw_by_id.get(str(event.get("event_id")))
                event_type = str(raw.get("event_type") or "") if isinstance(raw, Mapping) else ""
            elif event_type == "ANCHOR_LIFECYCLE_EVENT":
                raw = anchor_raw_by_id.get(str(event.get("event_id")))
                event_type = str(raw.get("event_type") or "") if isinstance(raw, Mapping) else ""
            if event_type in MATERIAL_TIMELINE_EVENTS:
                material.append({**event, "event_type": event_type})
        material.extend(program_setup_events(previous, ledger))
        material.extend(program_protection_events(previous, ledger))
        material.extend(program_course_state_events(previous, ledger))

        evidence = ledger.get("quadrant_evidence")
        evidence = evidence if isinstance(evidence, Mapping) else {}
        background = _recommended_quadrant(evidence.get("background_20bars"))
        working = _recommended_quadrant(evidence.get("working_5bars"))
        if background is not None:
            if stable_background is None:
                stable_background = background
                background_changed_at = expected
            elif background == stable_background:
                background_candidate = None
                background_candidate_count = 0
            else:
                if background == background_candidate:
                    background_candidate_count += 1
                else:
                    background_candidate = background
                    background_candidate_count = 1
                stable_long_enough = (
                    background_changed_at is None
                    or expected - background_changed_at >= timedelta(minutes=15)
                )
                if background_candidate_count >= 3 and stable_long_enough:
                    prior = stable_background
                    stable_background = background
                    background_changed_at = expected
                    background_candidate = None
                    background_candidate_count = 0
                    material.append(
                        {
                            "event_type": "BACKGROUND_QUADRANT_BASELINE_CHANGED",
                            "event_id": _id("E", "BACKGROUND_QUADRANT", prior, background, expected.isoformat()),
                            "event_time": expected.isoformat(),
                            "from": prior,
                            "to": background,
                            "recorded_at": expected.isoformat(),
                        }
                    )
        if working is not None:
            if stable_working is None:
                stable_working = working
                working_changed_at = expected
            elif working == stable_working:
                working_candidate = None
                working_candidate_count = 0
            else:
                if working == working_candidate:
                    working_candidate_count += 1
                else:
                    working_candidate = working
                    working_candidate_count = 1
                stable_long_enough = (
                    working_changed_at is None
                    or expected - working_changed_at >= timedelta(minutes=5)
                )
                if working_candidate_count >= 3 and stable_long_enough:
                    prior = stable_working
                    stable_working = working
                    working_changed_at = expected
                    working_candidate = None
                    working_candidate_count = 0
                    material.append(
                        {
                            "event_type": "WORKING_QUADRANT_BASELINE_CHANGED",
                            "event_id": _id("E", "WORKING_QUADRANT", prior, working, expected.isoformat()),
                            "event_time": expected.isoformat(),
                            "from": prior,
                            "to": working,
                            "recorded_at": expected.isoformat(),
                        }
                    )
        anchor_ref = (ledger.get("control_candidates") or {}).get("forming_small_anchor_ref")
        legs_by_id = _items_by_id(ledger, "legs")
        anchor_leg = legs_by_id.get(str(anchor_ref)) if anchor_ref is not None else None
        anchor = (
            str(anchor_leg.get("start_pivot_id"))
            if isinstance(anchor_leg, Mapping) and anchor_leg.get("start_pivot_id")
            else None
        )
        anchor_amplitude = (
            abs(float(anchor_leg.get("amplitude_points") or 0))
            if isinstance(anchor_leg, Mapping)
            else 0.0
        )
        anchor_atr = (ledger.get("indicators") or {}).get("atr14")
        anchor_is_material = (
            isinstance(anchor_atr, (int, float))
            and not isinstance(anchor_atr, bool)
            and anchor_amplitude >= max(30.0, float(anchor_atr) * 2.0)
        )
        if not anchor_lifecycle_enabled and anchor is not None and anchor_is_material and anchor != previous_anchor:
            event_type = (
                "WORKING_ANCHOR_ESTABLISHED"
                if previous_anchor is None
                else "WORKING_ANCHOR_CHANGED"
            )
            material.append(
                {
                    "event_type": event_type,
                    "event_id": _id(
                        "E",
                        event_type,
                        str(previous_anchor or "NONE"),
                        str(anchor),
                        expected.isoformat(),
                    ),
                    "event_time": expected.isoformat(),
                    "from": previous_anchor,
                    "to": anchor,
                    "recorded_at": expected.isoformat(),
                }
            )
        if material:
            timeline.append(
                {
                    "bar_time": expected.isoformat(),
                    "events": sorted(
                        material,
                        key=lambda item: (str(item.get("event_time")), str(item.get("event_type")), str(item.get("event_id"))),
                    ),
                }
            )
        if not anchor_lifecycle_enabled and anchor is not None:
            previous_anchor = str(anchor)
        previous = ledger
    return timeline


def program_setup_events(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Publish the first causal bar of each program-owned executable setup.

    A continuation setup can become executable without creating a new pivot,
    anchor or quadrant event on that same cutoff.  Keeping this lifecycle in
    the objective timeline prevents an event-driven replay from waiting for an
    unrelated later structure event before it asks the analyzer to explain the
    already armed plan.
    """

    current_levels = current.get("trade_levels")
    previous_levels = previous.get("trade_levels") if isinstance(previous, Mapping) else None
    candidate = (
        current_levels.get("continuation_arm_candidate")
        if isinstance(current_levels, Mapping)
        else None
    )
    prior = (
        previous_levels.get("continuation_arm_candidate")
        if isinstance(previous_levels, Mapping)
        else None
    )
    if not isinstance(candidate, Mapping) or not candidate.get("setup_key"):
        return []
    setup_key = str(candidate["setup_key"])
    prior_key = str(prior.get("setup_key")) if isinstance(prior, Mapping) and prior.get("setup_key") else None
    if prior_key == setup_key:
        return []
    event_type = "PROGRAM_SETUP_ARMED" if prior_key is None else "PROGRAM_SETUP_CHANGED"
    event_time = candidate.get("first_seen_at") or current.get("as_of")
    return [
        {
            "event_type": event_type,
            "event_id": _id("E", event_type, prior_key or "NONE", setup_key, str(event_time)),
            "event_time": event_time,
            "recorded_at": current.get("as_of"),
            "setup_key": setup_key,
            "prior_setup_key": prior_key,
            "direction": candidate.get("direction"),
        }
    ]


def program_protection_events(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Publish every newly observable structural-defense stop candidate.

    The timeline retains the event even when that defense is broken by a later
    cutoff.  A live position can therefore apply the stop from the exact close
    where it first became known instead of seeing only whichever defense
    happens to remain active at the next analyzer call.
    """

    current_levels = current.get("trade_levels")
    previous_levels = previous.get("trade_levels") if isinstance(previous, Mapping) else None
    current_candidates = (
        current_levels.get("position_protection_candidates")
        if isinstance(current_levels, Mapping)
        else None
    )
    previous_candidates = (
        previous_levels.get("position_protection_candidates")
        if isinstance(previous_levels, Mapping)
        else None
    )
    result: list[dict[str, Any]] = []
    for side in ("LONG", "SHORT"):
        candidate = current_candidates.get(side) if isinstance(current_candidates, Mapping) else None
        prior = previous_candidates.get(side) if isinstance(previous_candidates, Mapping) else None
        if not isinstance(candidate, Mapping) or not candidate.get("source_defense_id"):
            continue
        source_id = str(candidate["source_defense_id"])
        prior_id = (
            str(prior.get("source_defense_id"))
            if isinstance(prior, Mapping) and prior.get("source_defense_id")
            else None
        )
        if source_id == prior_id:
            continue
        event_time = candidate.get("defense_first_seen_at") or current.get("as_of")
        result.append(
            {
                "event_type": "PROGRAM_DEFENSE_AVAILABLE",
                "event_id": _id("E", "PROGRAM_DEFENSE_AVAILABLE", side, source_id, str(event_time)),
                "event_time": event_time,
                "recorded_at": current.get("as_of"),
                "direction": side,
                "source_anchor_id": candidate.get("source_anchor_id"),
                "source_defense_id": source_id,
                "source_time": candidate.get("source_time"),
                "source_price": candidate.get("source_price"),
                "stop_price": candidate.get("stop_price"),
                "atr_buffer_points": candidate.get("atr_buffer_points"),
            }
        )
    return result


def program_course_state_events(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Publish deterministic quadrant and Taiji state transitions.

    These events are based only on the causal prefix.  They make event-driven
    analysis observe the exact bar where the program verdict changes, and they
    are also part of the cross-model reproducibility fingerprint.
    """

    current_lifecycle = current.get("anchor_lifecycle")
    previous_lifecycle = previous.get("anchor_lifecycle") if isinstance(previous, Mapping) else None
    if not isinstance(current_lifecycle, Mapping):
        return []
    at = str(current.get("as_of") or "")
    result: list[dict[str, Any]] = []

    current_quadrant = current_lifecycle.get("quadrant_context")
    previous_quadrant = (
        previous_lifecycle.get("quadrant_context")
        if isinstance(previous_lifecycle, Mapping)
        else None
    )
    q_fields = (
        "background_primary",
        "background_trend_dynamics",
        "background_volatility_dynamics",
        "working_primary",
        "working_trend_dynamics",
        "working_volatility_dynamics",
    )
    current_q = tuple(current_quadrant.get(key) for key in q_fields) if isinstance(current_quadrant, Mapping) else None
    previous_q = tuple(previous_quadrant.get(key) for key in q_fields) if isinstance(previous_quadrant, Mapping) else None
    if current_q is not None and current_q != previous_q:
        result.append(
            {
                "event_type": "PROGRAM_QUADRANT_CHANGED",
                "event_id": _id("E", "PROGRAM_QUADRANT_CHANGED", *[str(item) for item in current_q], at),
                "event_time": at,
                "recorded_at": at,
                "from": list(previous_q) if previous_q is not None else None,
                "to": list(current_q),
                "authority": current_quadrant.get("authority"),
            }
        )

    current_taiji = current_lifecycle.get("taiji_context")
    previous_taiji = (
        previous_lifecycle.get("taiji_context")
        if isinstance(previous_lifecycle, Mapping)
        else None
    )
    t_fields = ("program_state", "program_quality", "last_copy_status", "engine_mode")
    current_t = tuple(current_taiji.get(key) for key in t_fields) if isinstance(current_taiji, Mapping) else None
    previous_t = tuple(previous_taiji.get(key) for key in t_fields) if isinstance(previous_taiji, Mapping) else None
    if current_t is not None and current_t != previous_t:
        result.append(
            {
                "event_type": "PROGRAM_TAIJI_CHANGED",
                "event_id": _id("E", "PROGRAM_TAIJI_CHANGED", *[str(item) for item in current_t], at),
                "event_time": at,
                "recorded_at": at,
                "from": list(previous_t) if previous_t is not None else None,
                "to": list(current_t),
                "authority": current_taiji.get("assessment_authority"),
                "operating_anchor_ref": current_taiji.get("dynasty_anchor_ref"),
            }
        )
    current_methods = current.get("course_method_state")
    previous_methods = previous.get("course_method_state") if isinstance(previous, Mapping) else None
    if isinstance(current_methods, Mapping):
        current_yizhi = current_methods.get("yizhi")
        current_left = current_methods.get("left_right")
        current_numbered = current_methods.get("numbered_market")
        previous_yizhi = previous_methods.get("yizhi") if isinstance(previous_methods, Mapping) else None
        previous_left = previous_methods.get("left_right") if isinstance(previous_methods, Mapping) else None
        previous_numbered = (
            previous_methods.get("numbered_market")
            if isinstance(previous_methods, Mapping)
            else None
        )
        current_numbered_key = (
            current_numbered.get("label") if isinstance(current_numbered, Mapping) else None,
            current_numbered.get("direction") if isinstance(current_numbered, Mapping) else None,
            current_numbered.get("last_changed_at") if isinstance(current_numbered, Mapping) else None,
        )
        previous_numbered_key = (
            previous_numbered.get("label") if isinstance(previous_numbered, Mapping) else None,
            previous_numbered.get("direction") if isinstance(previous_numbered, Mapping) else None,
            previous_numbered.get("last_changed_at") if isinstance(previous_numbered, Mapping) else None,
        )
        numbered_material = bool(
            isinstance(current_numbered, Mapping)
            and current_numbered.get("status") == "NUMBERED"
        ) or bool(
            isinstance(previous_numbered, Mapping)
            and previous_numbered.get("status") == "NUMBERED"
        )
        if numbered_material and current_numbered_key != previous_numbered_key:
            result.append(
                {
                    "event_type": "PROGRAM_NUMBERED_MARKET_CHANGED",
                    "event_id": _id(
                        "E", "PROGRAM_NUMBERED_MARKET_CHANGED",
                        *[str(item) for item in current_numbered_key], at,
                    ),
                    "event_time": at,
                    "recorded_at": at,
                    "from": list(previous_numbered_key),
                    "to": list(current_numbered_key),
                    "number": (
                        current_numbered.get("number")
                        if isinstance(current_numbered, Mapping)
                        else None
                    ),
                    "direction": (
                        current_numbered.get("direction")
                        if isinstance(current_numbered, Mapping)
                        else None
                    ),
                    "authority": (
                        current_numbered.get("authority")
                        if isinstance(current_numbered, Mapping)
                        else current_methods.get("authority")
                    ),
                }
            )
        current_method_key = (
            current_methods.get("cclass_mode"),
            current_yizhi.get("state") if isinstance(current_yizhi, Mapping) else None,
            current_yizhi.get("direction") if isinstance(current_yizhi, Mapping) else None,
            current_left.get("state") if isinstance(current_left, Mapping) else None,
            current_left.get("direction") if isinstance(current_left, Mapping) else None,
            *current_numbered_key[:2],
            current_methods.get("x_stage"),
        )
        previous_method_key = (
            previous_methods.get("cclass_mode") if isinstance(previous_methods, Mapping) else None,
            previous_yizhi.get("state") if isinstance(previous_yizhi, Mapping) else None,
            previous_yizhi.get("direction") if isinstance(previous_yizhi, Mapping) else None,
            previous_left.get("state") if isinstance(previous_left, Mapping) else None,
            previous_left.get("direction") if isinstance(previous_left, Mapping) else None,
            *previous_numbered_key[:2],
            previous_methods.get("x_stage") if isinstance(previous_methods, Mapping) else None,
        )
        if current_method_key != previous_method_key:
            result.append(
                {
                    "event_type": "PROGRAM_METHOD_CHANGED",
                    "event_id": _id(
                        "E", "PROGRAM_METHOD_CHANGED",
                        *[str(item) for item in current_method_key], at,
                    ),
                    "event_time": at,
                    "recorded_at": at,
                    "from": list(previous_method_key),
                    "to": list(current_method_key),
                    "authority": current_methods.get("authority"),
                }
            )
    return result


def _recommended_quadrant(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    result = value.get("recommended_quadrant")
    return str(result) if result in {"Q1", "Q2", "Q3", "Q4"} else None


def _validated_contiguous_bars(bars: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    previous: datetime | None = None
    for raw in bars:
        if not isinstance(raw, Mapping):
            continue
        at = _aware(raw.get("time"), "bar.time")
        if previous is not None and at - previous != timedelta(minutes=1):
            raise DeterministicReplayError("事件驅動回放只接受同一時段連續1分K。")
        item = dict(raw)
        for key in ("open", "high", "low", "close"):
            item[key] = _positive_number(item.get(key), f"bar.{key}")
        volume = item.get("volume")
        if isinstance(volume, (int, float)) and not isinstance(volume, bool):
            item["volume"] = float(volume)
        item["time"] = at.isoformat()
        result.append(item)
        previous = at
    if not result:
        raise DeterministicReplayError("事件驅動回放沒有可用K棒。")
    return result


def _causal_structured_snapshot(bars: list[dict[str, Any]]) -> dict[str, Any]:
    candles = [
        Candle(
            at=_aware(item["time"], "bar.time"),
            open=float(item["open"]),
            high=float(item["high"]),
            low=float(item["low"]),
            close=float(item["close"]),
        )
        for item in bars
    ]
    local, _stats = detect_local_pivots(candles, 2) if len(candles) >= 5 else ([], {})
    paired, _replacements, _ignored = pair_pivots(local)
    finalized = paired[:-1]
    large = secondary_pivots(finalized)
    latest = bars[-1]
    return {
        "latest_closed_k": {
            key: latest.get(key) for key in ("time", "open", "high", "low", "close", "volume")
        },
        "indicators": {
            "sma21": _simple_moving_average(candles, 21),
            "sma105": _simple_moving_average(candles, 105),
            "atr14": _wilder_atr(candles, 14),
        },
        "opening_ranges": {
            "or5": _opening_range_from_bars(bars, 5),
            "or15": _opening_range_from_bars(bars, 15),
        },
        "causal_structure_n2": {
            "dow_small": dow_state(finalized),
            "dow_large": dow_state(large),
            "recent_confirmed_pivots": [_local_pivot_view(item) for item in finalized[-6:]],
            "recent_large_pivots": [_local_pivot_view(item) for item in large[-4:]],
        },
    }


def _local_pivot_view(value: Any) -> dict[str, Any]:
    return {
        "kind": value.kind,
        "bar_time": value.bar_time,
        "confirmation_time": value.confirmation_time,
        "price": float(value.price),
    }


def _simple_moving_average(candles: list[Candle], period: int) -> float | None:
    if len(candles) < period:
        return None
    return round(sum(item.close for item in candles[-period:]) / period, 4)


def _wilder_atr(candles: list[Candle], period: int) -> float | None:
    if len(candles) < period + 1:
        return None
    true_ranges = [
        max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        for previous, current in zip(candles, candles[1:])
    ]
    atr = sum(true_ranges[:period]) / period
    for value in true_ranges[period:]:
        atr = ((period - 1) * atr + value) / period
    return round(atr, 4)


def _atr_from_bar_records(bars: list[dict[str, Any]], period: int) -> float | None:
    candles = [
        Candle(
            at=_aware(item["time"], "bar.time"),
            open=float(item["open"]),
            high=float(item["high"]),
            low=float(item["low"]),
            close=float(item["close"]),
        )
        for item in bars
    ]
    return _wilder_atr(candles, period)


def _opening_range_from_bars(bars: list[dict[str, Any]], minutes: int) -> dict[str, Any] | None:
    if len(bars) < minutes:
        return None
    window = bars[:minutes]
    high_bar = max(window, key=lambda item: float(item["high"]))
    low_bar = min(window, key=lambda item: float(item["low"]))
    return {
        "start": window[0]["time"],
        "end": window[-1]["time"],
        "high": float(high_bar["high"]),
        "high_time": high_bar["time"],
        "low": float(low_bar["low"]),
        "low_time": low_bar["time"],
    }


def allowed_references(ledger: Mapping[str, Any]) -> dict[str, set[str]]:
    result = {key: _ids(ledger, key) for key in ("pivots", "legs")}
    result["defenses"] = {
        str(item.get("id"))
        for item in ledger.get("defenses", [])
        if isinstance(item, Mapping) and item.get("id") and item.get("state") == "ACTIVE"
    }
    result["structure_events"] = _ids(ledger, "structure_events")
    result["anchor_records"] = _ids(ledger, "anchor_records")
    result["anchor_events"] = _ids(ledger, "anchor_events")
    result["anchor_defenses"] = {
        str(defense.get("id"))
        for key in ("background_anchor", "child_anchor")
        for anchor in [
            (ledger.get("anchor_lifecycle") or {}).get(key)
            if isinstance(ledger.get("anchor_lifecycle"), Mapping)
            else None
        ]
        for defense in [anchor.get("defense") if isinstance(anchor, Mapping) else None]
        if isinstance(defense, Mapping) and defense.get("id")
    }
    return result


def _grade_upgrade_evidence(
    pivots: list[dict[str, Any]],
    bars: list[dict[str, Any]],
    *,
    expected: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Detect a causal small-to-large structure upgrade.

    A bullish upgrade is observable when an already qualified small bullish
    defense is subsequently broken, the parent low survives, and a later
    closed bar reclaims the intervening high.  The bearish case is mirrored.
    The five-pivot shape alone is deliberately insufficient: the absorbed
    pivot must first have caused a favorable close beyond the preceding
    extreme, and the break must happen afterwards.  Nothing is backdated: the
    event becomes visible only after the replacement pivot itself has been
    confirmed and a closed bar crosses the old extreme.  The program publishes
    causal evidence; AI_HYBRID still decides whether the resulting structure
    truly has large-grade course significance.
    """

    events: list[dict[str, Any]] = []
    promoted: list[dict[str, Any]] = []
    if len(pivots) < 5 or not bars:
        return events, promoted
    ordered = sorted(pivots, key=lambda item: (item["bar_time"], item["first_seen_at"]))
    for index in range(len(ordered) - 4):
        window = ordered[index : index + 5]
        pivot_times = [
            _aware(item["bar_time"], "grade_upgrade.pivot.bar_time")
            for item in window
        ]
        if any(left >= right for left, right in zip(pivot_times, pivot_times[1:])):
            continue
        kinds = [item["kind"] for item in window]
        direction: str | None = None
        if kinds == ["LOW", "HIGH", "LOW", "HIGH", "LOW"]:
            parent, prior_boundary, absorbed, boundary, replacement = window
            if (
                float(boundary["price"]) > float(prior_boundary["price"])
                and float(replacement["price"]) < float(absorbed["price"])
                and float(replacement["price"]) > float(parent["price"])
            ):
                direction = "BULL"
        elif kinds == ["HIGH", "LOW", "HIGH", "LOW", "HIGH"]:
            parent, prior_boundary, absorbed, boundary, replacement = window
            if (
                float(boundary["price"]) < float(prior_boundary["price"])
                and float(replacement["price"]) > float(absorbed["price"])
                and float(replacement["price"]) < float(parent["price"])
            ):
                direction = "BEAR"
        if direction is None:
            continue
        absorbed_known_at = max(
            _aware(absorbed["first_seen_at"], "absorbed.first_seen_at"),
            _aware(absorbed["bar_time"], "absorbed.bar_time"),
        )
        replacement_at = _aware(replacement["bar_time"], "replacement.bar_time")
        defense_qualification = next(
            (
                bar
                for bar in bars
                if absorbed_known_at <= _aware(bar["time"], "bar.time") < replacement_at
                and (
                    float(bar["close"]) > float(prior_boundary["price"])
                    if direction == "BULL"
                    else float(bar["close"]) < float(prior_boundary["price"])
                )
            ),
            None,
        )
        if defense_qualification is None:
            # The absorbed pivot never became a causal Dow defense.  A later
            # cross through it therefore cannot be called a defense break or a
            # course grade upgrade.
            continue
        defense_qualified_at = _aware(
            defense_qualification["time"],
            "defense_qualification.time",
        )
        defense_breach = next(
            (
                bar
                for bar in bars
                if defense_qualified_at < _aware(bar["time"], "bar.time") <= replacement_at
                and (
                    float(bar["low"]) < float(absorbed["price"])
                    if direction == "BULL"
                    else float(bar["high"]) > float(absorbed["price"])
                )
            ),
            None,
        )
        if defense_breach is None:
            continue
        observable_from = max(
            _aware(replacement["first_seen_at"], "replacement.first_seen_at"),
            _aware(replacement["bar_time"], "replacement.bar_time"),
        )
        breakout = next(
            (
                bar
                for bar in bars
                if _aware(bar["time"], "bar.time") >= observable_from
                and (
                    float(bar["close"]) > float(boundary["price"])
                    if direction == "BULL"
                    else float(bar["close"]) < float(boundary["price"])
                )
            ),
            None,
        )
        if breakout is None:
            continue
        event_time = _aware(breakout["time"], "breakout.time")
        if event_time > expected:
            continue
        parent_failed = any(
            (
                float(bar["low"]) < float(parent["price"])
                if direction == "BULL"
                else float(bar["high"]) > float(parent["price"])
            )
            for bar in bars
            if defense_qualified_at < _aware(bar["time"], "bar.time") <= event_time
        )
        if parent_failed:
            continue
        event_id = _id(
            "S",
            "GRADE_UPGRADE",
            direction,
            parent["id"],
            absorbed["id"],
            replacement["id"],
            boundary["id"],
        )
        anchor_id = _id("L", "LARGE", direction, parent["bar_time"], "GRADE_UPGRADE")
        event = {
            "id": event_id,
            "event_type": "GRADE_UPGRADE",
            "direction": direction,
            "from_level": "SMALL",
            "to_level": "LARGE",
            "parent_origin_pivot_id": parent["id"],
            "absorbed_defense_pivot_id": absorbed["id"],
            "replacement_defense_pivot_id": replacement["id"],
            "reclaimed_boundary_pivot_id": boundary["id"],
            "parent_origin_time": parent["bar_time"],
            "absorbed_defense_time": absorbed["bar_time"],
            "replacement_defense_time": replacement["bar_time"],
            "reclaimed_boundary_time": boundary["bar_time"],
            "absorbed_defense_qualified_at": defense_qualified_at.isoformat(),
            "absorbed_defense_broken_at": _aware(
                defense_breach["time"],
                "defense_breach.time",
            ).isoformat(),
            "defense_break_evidence": "INTRABAR_EXTREME_CONFIRMED_AT_BAR_CLOSE",
            "parent_origin_price": parent["price"],
            "absorbed_defense_price": absorbed["price"],
            "replacement_defense_price": replacement["price"],
            "reclaimed_boundary_price": boundary["price"],
            "first_seen_at": event_time.isoformat(),
            "source_anchor_id": anchor_id,
            "qualification_status": "PROGRAM_CAUSAL_CANDIDATE",
            "grade_decision_authority": "AI_HYBRID",
        }
        if any(item["id"] == event_id for item in events):
            continue
        events.append(event)
        start_at = _aware(parent["bar_time"], "parent.bar_time")
        tail = [bar for bar in bars if start_at <= _aware(bar["time"], "bar.time") <= expected]
        if direction == "BULL":
            extreme = max(tail, key=lambda bar: (float(bar["high"]), str(bar["time"])))
            end_price = float(extreme["high"])
        else:
            extreme = min(tail, key=lambda bar: (float(bar["low"]), str(bar["time"])))
            end_price = float(extreme["low"])
        promoted_leg = _leg(
            level="LARGE",
            direction=direction,
            start_time=parent["bar_time"],
            start_price=float(parent["price"]),
            end_time=_aware(extreme["time"], "extreme.time").isoformat(),
            end_price=end_price,
            first_seen_at=event_time.isoformat(),
            status="FORMING",
            start_pivot_id=parent["id"],
            end_pivot_id=None,
        )
        promoted_leg.update(
            {
                "id": anchor_id,
                "source": "GRADE_UPGRADE",
                "source_event_id": event_id,
                "replacement_defense_pivot_id": replacement["id"],
            }
        )
        promoted.append(promoted_leg)
    events.sort(key=lambda item: (item["first_seen_at"], item["id"]))
    promoted.sort(key=lambda item: (item["first_seen_at"], item["id"]))
    return events[-4:], promoted[-4:]


def _grade_lifecycle_events(
    upgrades: list[dict[str, Any]],
    bars: list[dict[str, Any]],
    *,
    expected: datetime,
) -> list[dict[str, Any]]:
    """Track a promoted structure after its upgrade without guessing a new trend.

    Crossing the replacement defense downgrades the promoted structure. Crossing
    its parent origin invalidates it. Both events are closed-bar facts and do not
    by themselves authorize a reverse trade.
    """

    result: list[dict[str, Any]] = []
    for upgrade in upgrades:
        direction = str(upgrade.get("direction") or "")
        if direction not in {"BULL", "BEAR"}:
            continue
        first_seen = _aware(upgrade.get("first_seen_at"), "upgrade.first_seen_at")
        replacement = _positive_number(
            upgrade.get("replacement_defense_price"),
            "upgrade.replacement_defense_price",
        )
        parent = _positive_number(upgrade.get("parent_origin_price"), "upgrade.parent_origin_price")
        available = [
            item
            for item in bars
            if first_seen < _aware(item.get("time"), "bar.time") <= expected
        ]
        downgrade = next(
            (
                item
                for item in available
                if (
                    float(item["close"]) < replacement
                    if direction == "BULL"
                    else float(item["close"]) > replacement
                )
            ),
            None,
        )
        if downgrade is not None:
            result.append(
                {
                    "id": _id("S", "GRADE_DOWNGRADE", str(upgrade["id"])),
                    "event_type": "GRADE_DOWNGRADE",
                    "direction": direction,
                    "from_level": "LARGE",
                    "to_level": "SMALL",
                    "source_upgrade_event_id": upgrade["id"],
                    "source_anchor_id": upgrade.get("source_anchor_id"),
                    "broken_defense_price": replacement,
                    "parent_origin_price": parent,
                    "close_price": float(downgrade["close"]),
                    "first_seen_at": _aware(downgrade["time"], "downgrade.time").isoformat(),
                }
            )
        invalidation = next(
            (
                item
                for item in available
                if (
                    float(item["close"]) < parent
                    if direction == "BULL"
                    else float(item["close"]) > parent
                )
            ),
            None,
        )
        if invalidation is not None:
            result.append(
                {
                    "id": _id("S", "STRUCTURE_INVALIDATED", str(upgrade["id"])),
                    "event_type": "STRUCTURE_INVALIDATED",
                    "direction": direction,
                    "source_upgrade_event_id": upgrade["id"],
                    "source_anchor_id": upgrade.get("source_anchor_id"),
                    "broken_parent_price": parent,
                    "close_price": float(invalidation["close"]),
                    "first_seen_at": _aware(invalidation["time"], "invalidation.time").isoformat(),
                }
            )
    return result


def _false_break_reclaim_events(
    *,
    pivots: list[dict[str, Any]],
    defenses: list[dict[str, Any]],
    opening_ranges: Any,
    bars: list[dict[str, Any]],
    expected: datetime,
    max_reclaim_bars: int = 5,
) -> list[dict[str, Any]]:
    """Find closed-bar false breaks at levels already known before the breach."""

    candidates: dict[tuple[str, float], dict[str, Any]] = {}

    def register(
        direction: str,
        price: Any,
        known_at: Any,
        source_type: str,
        source_id: str,
        priority: int,
        valid_until: Any = None,
    ) -> None:
        if direction not in {"BULL", "BEAR"}:
            return
        if isinstance(price, bool) or not isinstance(price, (int, float)) or float(price) <= 0:
            return
        if not isinstance(known_at, str) or not known_at:
            return
        numeric = _positive_number(price, "false_break.level_price")
        known = _aware(known_at, "false_break.known_at")
        if known > expected:
            return
        expires = _aware(valid_until, "false_break.valid_until") if isinstance(valid_until, str) and valid_until else None
        key = (direction, round(numeric, 4))
        existing = candidates.get(key)
        if existing is None or priority > int(existing["priority"]):
            candidates[key] = {
                "direction": direction,
                "level_price": numeric,
                "known_at": known,
                "source_type": source_type,
                "source_id": source_id,
                "priority": priority,
                "valid_until": expires,
            }

    for item in pivots:
        if item.get("kind") == "LOW":
            register("BULL", item.get("price"), item.get("first_seen_at"), "PIVOT_LOW", str(item.get("id")), 2)
        elif item.get("kind") == "HIGH":
            register("BEAR", item.get("price"), item.get("first_seen_at"), "PIVOT_HIGH", str(item.get("id")), 2)
    for item in defenses:
        register(
            str(item.get("direction")),
            item.get("price"),
            item.get("first_seen_at"),
            "DOW_DEFENSE",
            str(item.get("id")),
            3,
            (
                item.get("broken_at")
                if item.get("state") in {"BROKEN", "BROKEN_AND_RECLAIMED"}
                else None
            ),
        )
    if isinstance(opening_ranges, Mapping):
        for name, value in opening_ranges.items():
            if not isinstance(value, Mapping) or not value.get("end"):
                continue
            register("BULL", value.get("low"), value.get("end"), f"{str(name).upper()}_LOW", str(name), 1)
            register("BEAR", value.get("high"), value.get("end"), f"{str(name).upper()}_HIGH", str(name), 1)

    ordered_bars = sorted(bars, key=lambda item: str(item.get("time")))
    result: list[dict[str, Any]] = []
    for candidate in candidates.values():
        direction = candidate["direction"]
        level = float(candidate["level_price"])
        cursor = 1
        while cursor < len(ordered_bars):
            previous = ordered_bars[cursor - 1]
            breach = ordered_bars[cursor]
            breach_at = _aware(breach["time"], "breach.time")
            previous_at = _aware(previous["time"], "previous.time")
            if breach_at - previous_at != timedelta(minutes=1):
                cursor += 1
                continue
            if breach_at < candidate["known_at"] or breach_at > expected:
                cursor += 1
                continue
            # A Dow defense may produce a false-break event on its first
            # closed-bar breach, but once that breach has expired without a
            # timely reclaim it cannot later be reused as a fresh boundary.
            # This prevents an absorbed/broken defense from resurrecting a
            # setup after the structure has already moved on.
            if candidate.get("valid_until") is not None and breach_at > candidate["valid_until"]:
                cursor += 1
                continue
            if breach_at - candidate["known_at"] < timedelta(minutes=2):
                cursor += 1
                continue
            if candidate["source_type"] not in {"DOW_DEFENSE", "OR5_LOW", "OR5_HIGH", "OR15_LOW", "OR15_HIGH"}:
                newer_same_side = [
                    other
                    for other in candidates.values()
                    if other["direction"] == direction
                    and candidate["known_at"] < other["known_at"] <= breach_at
                ]
                if newer_same_side:
                    cursor += 1
                    continue
            crossed = (
                float(previous["close"]) >= level and float(breach["close"]) < level
                if direction == "BULL"
                else float(previous["close"]) <= level and float(breach["close"]) > level
            )
            if not crossed:
                cursor += 1
                continue
            search_end = min(len(ordered_bars), cursor + max_reclaim_bars + 1)
            reclaim_index = None
            reclaim = None
            extreme = None
            for index in range(cursor + 1, search_end):
                item = ordered_bars[index]
                inside = (
                    float(item["close"]) > level
                    if direction == "BULL"
                    else float(item["close"]) < level
                )
                if not inside:
                    continue
                causal_slice = ordered_bars[cursor - 1 : index + 1]
                causal_times = [_aware(bar["time"], "bar.time") for bar in causal_slice]
                if any(
                    right - left != timedelta(minutes=1)
                    for left, right in zip(causal_times, causal_times[1:])
                ):
                    break
                excursion = ordered_bars[cursor : index + 1]
                candidate_extreme = (
                    min(float(bar["low"]) for bar in excursion)
                    if direction == "BULL"
                    else max(float(bar["high"]) for bar in excursion)
                )
                event_atr = _atr_from_bar_records(ordered_bars[: index + 1], 14)
                reclaim_distance = abs(float(item["close"]) - level)
                excursion_distance = abs(candidate_extreme - level)
                if event_atr is not None and (
                    reclaim_distance < event_atr * 0.4
                    or excursion_distance < event_atr * 0.1
                ):
                    # A shallow first recross is not yet a useful false-break
                    # confirmation.  The course allows a little back-and-forth
                    # around the boundary, so keep the original breach alive
                    # for the bounded window and accept the first decisive
                    # reclaim rather than discarding the event permanently.
                    continue
                reclaim_index = index
                reclaim = item
                extreme = candidate_extreme
                break
            if reclaim_index is None or reclaim is None or extreme is None:
                cursor += 1
                continue
            reclaim_at = _aware(reclaim["time"], "reclaim.time")
            if reclaim_at > expected:
                break
            event_id = _id(
                "S",
                "FALSE_BREAK_RECLAIM",
                direction,
                str(candidate["source_id"]),
                breach_at.isoformat(),
            )
            result.append(
                {
                    "id": event_id,
                    "event_type": "FALSE_BREAK_RECLAIM",
                    "direction": direction,
                    "source_type": candidate["source_type"],
                    "source_id": candidate["source_id"],
                    "level_price": level,
                    "breach_time": breach_at.isoformat(),
                    "breach_close": float(breach["close"]),
                    "breach_extreme": extreme,
                    "reclaim_close": float(reclaim["close"]),
                    "bars_to_reclaim": reclaim_index - cursor,
                    "first_seen_at": reclaim_at.isoformat(),
                }
            )
            cursor = reclaim_index + 1
    result.sort(key=lambda item: (item["first_seen_at"], item["id"]))
    return result[-12:]


def _merge_course_lifecycle_defenses(
    existing: list[dict[str, Any]],
    anchor_state: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Add the course-authoritative Dow slots to the public evidence ledger."""

    merged = {str(item.get("id")): dict(item) for item in existing if item.get("id")}
    context = anchor_state.get("dow_context")
    if not isinstance(context, Mapping):
        return list(merged.values())
    for grade in ("large", "small"):
        for side in ("bull", "bear"):
            value = context.get(f"{grade}_{side}_defense")
            if not isinstance(value, Mapping) or not value.get("id"):
                continue
            item = dict(value)
            item["level"] = str(item.get("level") or grade.upper())
            item["direction"] = str(item.get("direction") or side.upper())
            item["bar_time"] = item.get("bar_time") or item.get("time")
            prior = merged.get(str(item["id"]))
            item["first_seen_at"] = (
                item.get("first_seen_at")
                or (prior.get("first_seen_at") if isinstance(prior, Mapping) else None)
                or anchor_state.get("as_of")
            )
            merged[str(item["id"])] = item
    return sorted(
        merged.values(),
        key=lambda item: (
            str(item.get("first_seen_at") or ""),
            str(item.get("level") or ""),
            str(item.get("id") or ""),
        ),
    )


def _deterministic_trade_levels(
    *,
    pivots: list[dict[str, Any]],
    structure_events: list[dict[str, Any]],
    indicators: Any,
    latest: Mapping[str, Any],
    expected: datetime,
) -> dict[str, Any]:
    """Publish reproducible stop candidates; the analyzer still decides entry."""

    raw_indicators = indicators if isinstance(indicators, Mapping) else {}
    atr = raw_indicators.get("atr14")
    buffer = round(max(1.0, float(atr) * 0.2), 1) if isinstance(atr, (int, float)) and not isinstance(atr, bool) else None
    latest_close = latest.get("close")

    def structural(direction: str) -> dict[str, Any] | None:
        kind = "LOW" if direction == "BULL" else "HIGH"
        eligible = [
            item
            for item in pivots
            if item.get("kind") == kind
            and _aware(item.get("first_seen_at"), "pivot.first_seen_at") <= expected
            and isinstance(latest_close, (int, float))
            and (
                float(item["price"]) < float(latest_close)
                if direction == "BULL"
                else float(item["price"]) > float(latest_close)
            )
        ]
        if not eligible:
            return None
        pivot = max(eligible, key=lambda item: (str(item.get("bar_time")), str(item.get("first_seen_at"))))
        stop = None
        if buffer is not None:
            stop = float(pivot["price"]) - buffer if direction == "BULL" else float(pivot["price"]) + buffer
        return {
            "direction": direction,
            "source_type": "CONFIRMED_OR_WORKING_PIVOT",
            "source_id": pivot["id"],
            "source_time": pivot["bar_time"],
            "source_price": float(pivot["price"]),
            "atr_buffer_points": buffer,
            "stop_price": None if stop is None else round(stop, 1),
            "eligible_from": pivot["first_seen_at"],
        }

    false_breaks = [item for item in structure_events if item.get("event_type") == "FALSE_BREAK_RECLAIM"]
    latest_false = false_breaks[-1] if false_breaks else None
    reentry: dict[str, Any] | None = None
    if isinstance(latest_false, Mapping):
        direction = str(latest_false.get("direction"))
        extreme = float(latest_false["breach_extreme"])
        stop = None
        if buffer is not None:
            stop = extreme - buffer if direction == "BULL" else extreme + buffer
        reentry = {
            "direction": direction,
            "source_event_id": latest_false["id"],
            "reclaimed_level": float(latest_false["level_price"]),
            "breach_extreme": extreme,
            "atr_buffer_points": buffer,
            "stop_price": None if stop is None else round(stop, 1),
            "eligible_from": latest_false["first_seen_at"],
        }
    return {
        "long_structural_stop": structural("BULL"),
        "short_structural_stop": structural("BEAR"),
        "latest_false_break_reentry": reentry,
        "authority": "REFERENCE_NOT_STRATEGY_SPECIFIC_STOP",
        "buffer_policy": "ENGINEERING_REFERENCE_0_2_ATR_NOT_COURSE_RULE",
    }


def _position_protection_candidates(
    anchor_state: Mapping[str, Any],
    *,
    bars: list[dict[str, Any]],
) -> dict[str, Any]:
    """Lock each active defense's ATR buffer at its first observable bar.

    A later ATR contraction must not move an existing stop by fractions of a
    point.  Only a newly completed defense can publish a different candidate.
    """

    result: dict[str, Any] = {"LONG": None, "SHORT": None}
    sources: list[tuple[Mapping[str, Any], Any, str | None, str | None]] = []
    for anchor in (anchor_state.get("background_anchor"), anchor_state.get("child_anchor")):
        if isinstance(anchor, Mapping) and isinstance(anchor.get("defense"), Mapping):
            sources.append(
                (
                    anchor["defense"],
                    anchor.get("id"),
                    anchor.get("level"),
                    str(anchor.get("direction") or "") or None,
                )
            )
    dow = anchor_state.get("dow_context")
    if isinstance(dow, Mapping):
        for field in (
            "large_bull_defense",
            "large_bear_defense",
            "small_bull_defense",
            "small_bear_defense",
        ):
            defense = dow.get(field)
            if isinstance(defense, Mapping):
                field_direction = "BULL" if "bull" in field else "BEAR"
                sources.append(
                    (
                        defense,
                        defense.get("anchor_id"),
                        defense.get("level"),
                        str(defense.get("direction") or field_direction),
                    )
                )

    seen: set[tuple[str, str, float]] = set()
    for defense, source_anchor_id, source_level, source_direction in sources:
        if not isinstance(defense, Mapping) or defense.get("state") != "ACTIVE":
            continue
        direction = str(defense.get("direction") or source_direction or "")
        side = "LONG" if direction == "BULL" else "SHORT" if direction == "BEAR" else None
        if side is None:
            continue
        identity = (
            str(defense.get("id") or ""),
            str(defense.get("time") or ""),
            float(defense["price"]),
        )
        if identity in seen:
            continue
        seen.add(identity)
        # Directional Dow slots are not required to belong to the currently
        # published anchor.  Use the defense's own causal observation time;
        # falling back to its source bar time is safe for legacy fixtures.
        first_seen = defense.get("first_seen_at") or defense.get("time")
        if not isinstance(first_seen, str):
            continue
        visible_bars = [
            item for item in bars
            if _aware(item.get("time"), "bar.time") <= _aware(first_seen, "defense.first_seen_at")
        ]
        atr = _atr_from_bar_records(visible_bars, 14)
        buffer = round(max(1.0, float(atr) * 0.2), 1) if atr is not None else 1.0
        source_price = float(defense["price"])
        stop = source_price - buffer if side == "LONG" else source_price + buffer
        candidate = {
            "direction": side,
            "source_anchor_id": source_anchor_id,
            "source_defense_id": defense.get("id"),
            "source_level": source_level,
            "source_role": defense.get("role"),
            "source_time": defense.get("time"),
            "source_price": source_price,
            "defense_first_seen_at": first_seen,
            "atr_buffer_points": buffer,
            "stop_price": round(stop, 1),
            "buffer_locked_at": first_seen,
        }
        prior = result.get(side)
        more_protective = (
            not isinstance(prior, Mapping)
            or (
                side == "LONG"
                and float(candidate["stop_price"]) > float(prior["stop_price"])
            )
            or (
                side == "SHORT"
                and float(candidate["stop_price"]) < float(prior["stop_price"])
            )
        )
        if more_protective:
            result[side] = candidate
    return result


def apply_profit_milestone_protection(
    candidates: Mapping[str, Any] | None,
    *,
    position: Mapping[str, Any],
    bars: list[dict[str, Any]],
    as_of: str,
) -> dict[str, Any]:
    """Publish the one program-owned protection decision for this cutoff.

    A newly completed favorable defense may improve the original stop before
    +1.5R.  At +1.5R, near-cost protection outranks a weaker defense.  Both
    decisions become effective only at the current analysis cutoff, so neither
    can retroactively fill inside the bar that first revealed it.  Candidates
    that predate the position remain evidence only and can never be selected by
    the language model as a discretionary trailing stop.
    """

    result = {
        "LONG": None,
        "SHORT": None,
        **(dict(candidates) if isinstance(candidates, Mapping) else {}),
    }
    side = str(position.get("status") or "")
    if side not in {"LONG", "SHORT"}:
        return result
    entry = _number_or_none(position.get("entry_price"))
    current_stop = _number_or_none(position.get("stop_price"))
    entry_time = position.get("entry_time")
    if entry is None or current_stop is None or not isinstance(entry_time, str):
        return result
    behavior = position.get("behavior_plan")
    initial_stop = (
        _number_or_none(behavior.get("initial_stop_price"))
        if isinstance(behavior, Mapping)
        else None
    )
    if initial_stop is None:
        initial_stop = current_stop
    initial_risk = abs(entry - initial_stop)
    if initial_risk <= 0:
        return result

    cutoff = _aware(as_of, "as_of")
    started = _aware(entry_time, "entry_time")
    visible = [
        item
        for item in bars
        if started <= _aware(item.get("time"), "bar.time") <= cutoff
    ]
    if not visible:
        return result
    if side == "LONG":
        favorable_points = max(float(item["high"]) for item in visible) - entry
        latest_close = float(visible[-1]["close"])
        still_unprotected = current_stop < entry
        cost_stop_is_valid = latest_close > entry
    else:
        favorable_points = entry - min(float(item["low"]) for item in visible)
        latest_close = float(visible[-1]["close"])
        still_unprotected = current_stop > entry
        cost_stop_is_valid = latest_close < entry
    favorable_r = favorable_points / initial_risk

    structural = result.get(side)
    if isinstance(structural, Mapping):
        structural = dict(structural)
        structural_stop = _number_or_none(structural.get("stop_price"))
        defense_first_seen = structural.get("defense_first_seen_at")
        completed_after_entry = (
            isinstance(defense_first_seen, str)
            and _aware(defense_first_seen, "defense_first_seen_at") > started
        )
        improves_current = (
            structural_stop is not None
            and (
                (side == "LONG" and structural_stop > current_stop)
                or (side == "SHORT" and structural_stop < current_stop)
            )
        )
        valid_at_cutoff = (
            structural_stop is not None
            and (
                (side == "LONG" and structural_stop < latest_close)
                or (side == "SHORT" and structural_stop > latest_close)
            )
        )
        if completed_after_entry and improves_current and valid_at_cutoff:
            structural.update(
                {
                    "protection_reason": "NEW_FAVORABLE_DEFENSE",
                    "protection_required": True,
                    "initial_stop_price": round(initial_stop, 1),
                }
            )
        else:
            structural.update(
                {
                    "protection_reason": "REFERENCE_ONLY",
                    "protection_required": False,
                }
            )
        result[side] = structural

    if favorable_r < 1.5 or not still_unprotected or not cost_stop_is_valid:
        return result

    milestone = {
        "direction": side,
        "source_anchor_id": None,
        "source_defense_id": None,
        "source_time": as_of,
        "source_price": entry,
        "defense_first_seen_at": as_of,
        "atr_buffer_points": 0.0,
        "stop_price": round(entry, 1),
        "buffer_locked_at": as_of,
        "protection_reason": "PROFIT_MILESTONE_1_5R_NEAR_COST",
        "protection_required": True,
        "initial_stop_price": round(initial_stop, 1),
        "initial_risk_points": round(initial_risk, 1),
        "max_favorable_points": round(favorable_points, 1),
        "max_favorable_r": round(favorable_r, 4),
    }
    existing = result.get(side)
    existing_stop = (
        _number_or_none(existing.get("stop_price"))
        if isinstance(existing, Mapping)
        else None
    )
    existing_is_better = (
        existing_stop is not None
        and isinstance(existing, Mapping)
        and existing.get("protection_required") is True
        and (
            (side == "LONG" and existing_stop >= entry)
            or (side == "SHORT" and existing_stop <= entry)
        )
    )
    if not existing_is_better:
        result[side] = milestone
    return result


def _number_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _quadrant_evidence(
    bars: list[dict[str, Any]],
    indicators: Any,
) -> dict[str, Any]:
    """Window diagnostics only; these windows do not define structural grades."""

    raw_indicators = indicators if isinstance(indicators, Mapping) else {}
    ma21 = raw_indicators.get("sma21")
    return {
        "working_5bars": _window_quadrant_metrics(bars, movement_bars=5, ma21=ma21),
        "background_20bars": _window_quadrant_metrics(bars, movement_bars=20, ma21=ma21),
    }


def _window_quadrant_metrics(
    bars: list[dict[str, Any]],
    *,
    movement_bars: int,
    ma21: Any,
) -> dict[str, Any]:
    needed = movement_bars + 1
    if len(bars) < needed:
        return {
            "sample_bar_count": len(bars),
            "close_change": None,
            "directional_efficiency": None,
            "true_range_ratio_recent_to_prior": None,
            "latest_close_vs_21ma": "UNAVAILABLE",
        }
    tail = bars[-needed:]
    try:
        tail_times = [_aware(str(item["time"]), "bar.time") for item in tail]
    except (KeyError, TypeError, ValueError):
        tail_times = []
    if len(tail_times) != needed or any(
        right - left != timedelta(minutes=1)
        for left, right in zip(tail_times, tail_times[1:])
    ):
        return {
            "sample_bar_count": len(bars),
            "close_change": None,
            "directional_efficiency": None,
            "true_range_ratio_recent_to_prior": None,
            "latest_close_vs_21ma": "UNAVAILABLE",
        }
    closes = [float(item["close"]) for item in tail]
    move = closes[-1] - closes[0]
    path = sum(abs(right - left) for left, right in zip(closes, closes[1:]))
    ranges = [max(float(item["high"]) - float(item["low"]),
                  abs(float(item["high"]) - float(bars[index - 1]["close"])),
                  abs(float(item["low"]) - float(bars[index - 1]["close"])))
              if index else float(item["high"]) - float(item["low"])
              for index, item in enumerate(bars)]
    volatility_window = 3 if movement_bars == 5 else 10
    recent_slice = ranges[-volatility_window:]
    prior_slice = ranges[-2 * volatility_window : -volatility_window]
    recent = sum(recent_slice) / len(recent_slice)
    prior = sum(prior_slice) / len(prior_slice) if prior_slice else 0.0
    relation = "UNAVAILABLE"
    if isinstance(ma21, (int, float)) and not isinstance(ma21, bool):
        relation = "ABOVE" if closes[-1] > float(ma21) else "BELOW" if closes[-1] < float(ma21) else "AT"
    efficiency = round(abs(move) / path, 4) if path else 0.0
    ratio = round(recent / prior, 4) if prior else None
    return {
        "sample_bar_count": len(bars),
        "close_change": round(move, 4),
        "directional_efficiency": efficiency,
        "true_range_ratio_recent_to_prior": ratio,
        "latest_close_vs_21ma": relation,
        "recommended_trend_dynamics": None,
        "recommended_volatility_dynamics": None,
        "recommended_quadrant": None,
        "authority": "DIAGNOSTIC_ONLY_NOT_STRUCTURAL_GRADE",
    }


def _control_candidates(
    legs: list[dict[str, Any]],
    structure_events: list[dict[str, Any]],
) -> dict[str, Any]:
    def latest(level: str, status: str) -> str | None:
        matches = [
            item
            for item in legs
            if item.get("level") == level and item.get("status") == status
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: (str(item.get("end_time")), str(item.get("first_seen_at"))))["id"]

    return {
        "latest_confirmed_large_anchor_ref": latest("LARGE", "CONFIRMED"),
        "latest_confirmed_small_anchor_ref": latest("SMALL", "CONFIRMED"),
        "forming_large_anchor_ref": latest("LARGE", "FORMING"),
        "forming_small_anchor_ref": latest("SMALL", "FORMING"),
        "latest_structure_event_ref": structure_events[-1]["id"] if structure_events else None,
    }


def _pivot_group(value: Any, *, level: str, expected: datetime) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return result
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        kind = str(raw.get("kind") or "")
        if kind not in {"HIGH", "LOW"}:
            continue
        bar_time = _aware(raw.get("bar_time"), "pivot.bar_time")
        confirmation = _aware(raw.get("confirmation_time"), "pivot.confirmation_time")
        if confirmation > expected:
            raise DeterministicReplayError("因果樞紐的確認時間晚於回放截止時間。")
        price = _positive_number(raw.get("price"), "pivot.price")
        result.append(
            {
                "id": _id("P", level, kind, bar_time.isoformat()),
                "level": level,
                "kind": kind,
                "bar_time": bar_time.isoformat(),
                "first_seen_at": confirmation.isoformat(),
                "price": price,
            }
        )
    result.sort(key=lambda item: (item["bar_time"], item["first_seen_at"], item["kind"]))
    return result


def _working_pivot_group(
    bars: list[dict[str, Any]],
    *,
    expected: datetime,
) -> list[dict[str, Any]]:
    """Return the causal paired sequence including its last unpaired endpoint.

    structured_market_data intentionally publishes only ``sequence[:-1]`` as
    confirmed pivots.  The omitted endpoint is still causally observable after
    its n=2 and close-breach confirmation; keeping it here fixes the forming
    working anchor without presenting it as a fully paired pivot.
    """

    if len(bars) < 5:
        return []
    candles = [
        Candle(
            at=_aware(item["time"], "bar.time"),
            open=float(item["open"]),
            high=float(item["high"]),
            low=float(item["low"]),
            close=float(item["close"]),
        )
        for item in bars
    ]
    local, _stats = detect_local_pivots(candles, 2)
    sequence, _replacements, _ignored = pair_pivots(local)
    result: list[dict[str, Any]] = []
    for raw in sequence:
        confirmation = _aware(raw.confirmation_time, "working_pivot.confirmation_time")
        if confirmation > expected:
            continue
        result.append(
            {
                "id": _id("P", "SMALL", raw.kind, _aware(raw.bar_time, "working_pivot.bar_time").isoformat()),
                "level": "SMALL",
                "kind": raw.kind,
                "bar_time": _aware(raw.bar_time, "working_pivot.bar_time").isoformat(),
                "first_seen_at": confirmation.isoformat(),
                "price": float(raw.price),
                "state": "WORKING_ENDPOINT",
            }
        )
    return result


def _merge_pivot_evidence(
    confirmed: list[dict[str, Any]],
    working: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {item["id"]: item for item in confirmed}
    for item in working:
        by_id.setdefault(item["id"], item)
    return sorted(by_id.values(), key=lambda item: (item["bar_time"], item["first_seen_at"], item["kind"]))


def _completed_legs(pivots: list[dict[str, Any]], *, level: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for start, end in zip(pivots, pivots[1:]):
        if start["kind"] == end["kind"]:
            continue
        direction = "BULL" if start["kind"] == "LOW" else "BEAR"
        result.append(
            _leg(
                level=level,
                direction=direction,
                start_time=start["bar_time"],
                start_price=start["price"],
                end_time=end["bar_time"],
                end_price=end["price"],
                first_seen_at=end["first_seen_at"],
                status="CONFIRMED",
                start_pivot_id=start["id"],
                end_pivot_id=end["id"],
            )
        )
    return result


def _forming_leg(
    pivots: list[dict[str, Any]],
    bars: list[dict[str, Any]],
    *,
    level: str,
    expected: datetime,
) -> dict[str, Any] | None:
    if not pivots or not bars:
        return None
    start = pivots[-1]
    start_at = _aware(start["bar_time"], "start.bar_time")
    tail = [item for item in bars if _aware(item["time"], "bar.time") >= start_at]
    if not tail:
        return None
    direction = "BULL" if start["kind"] == "LOW" else "BEAR"
    origin_time = start["bar_time"]
    origin_price = float(start["price"])
    origin_pivot_id = start["id"]
    extreme_time = origin_time
    extreme_price = origin_price
    # A current leg continues until an opposite structural pivot is observed.
    # An unapproved 0.75 * average-range / 10-point gate must not create one.
    for bar in tail:
        bar_at = _aware(bar["time"], "bar.time").isoformat()
        if bar_at <= start_at.isoformat():
            continue
        price = float(bar["high"] if direction == "BULL" else bar["low"])
        if (price > extreme_price if direction == "BULL" else price < extreme_price):
            extreme_price = price
            extreme_time = bar_at
    result = _leg(
        level=level,
        direction=direction,
        start_time=origin_time,
        start_price=origin_price,
        end_time=extreme_time,
        end_price=extreme_price,
        first_seen_at=expected.isoformat(),
        status="FORMING",
        start_pivot_id=origin_pivot_id,
        end_pivot_id=None,
    )
    result["source"] = "CAUSAL_PIVOT_EXTENSION"
    return result


def _leg(
    *,
    level: str,
    direction: str,
    start_time: str,
    start_price: float,
    end_time: str,
    end_price: float,
    first_seen_at: str,
    status: str,
    start_pivot_id: str,
    end_pivot_id: str | None,
) -> dict[str, Any]:
    start = _aware(start_time, "leg.start_time")
    end = _aware(end_time, "leg.end_time")
    amplitude = end_price - start_price
    return {
        "id": _id("L", level, direction, start.isoformat(), end.isoformat(), status),
        "level": level,
        "direction": direction,
        "status": status,
        "start_time": start.isoformat(),
        "start_price": start_price,
        "end_time": end.isoformat(),
        "end_price": end_price,
        "amplitude_points": round(amplitude, 4),
        "duration_minutes": max(0, int((end - start).total_seconds() // 60)),
        "first_seen_at": _aware(first_seen_at, "leg.first_seen_at").isoformat(),
        "start_pivot_id": start_pivot_id,
        "end_pivot_id": end_pivot_id,
    }


def _defense(
    pivots: list[dict[str, Any]],
    bars: list[dict[str, Any]],
    *,
    level: str,
    dow: str,
) -> dict[str, Any] | None:
    kind = "LOW" if dow == "BULL" else "HIGH" if dow == "BEAR" else None
    if kind is None:
        return None
    pivot = next((item for item in reversed(pivots) if item["kind"] == kind), None)
    if pivot is None:
        return None
    direction = "BULL" if kind == "LOW" else "BEAR"
    confirmed_at = _aware(pivot["first_seen_at"], "defense.first_seen_at")
    broken = next(
        (
            item
            for item in bars
            if _aware(item["time"], "bar.time") >= confirmed_at
            and (
                float(item["close"]) < float(pivot["price"])
                if direction == "BULL"
                else float(item["close"]) > float(pivot["price"])
            )
        ),
        None,
    )
    return {
        "id": _id("D", level, direction, pivot["id"]),
        "level": level,
        "direction": direction,
        "source_pivot_id": pivot["id"],
        "bar_time": pivot["bar_time"],
        "first_seen_at": pivot["first_seen_at"],
        "price": pivot["price"],
        "state": "BROKEN" if broken is not None else "ACTIVE",
        "broken_at": None if broken is None else _aware(broken["time"], "bar.time").isoformat(),
    }


def _merge_defense_history(
    current: list[dict[str, Any]],
    *,
    previous_ledger: Mapping[str, Any] | None,
    bars: list[dict[str, Any]],
    session_key: str,
) -> list[dict[str, Any]]:
    by_id = {item["id"]: item for item in current}
    if not isinstance(previous_ledger, Mapping) or previous_ledger.get("session_key") != session_key:
        return current
    old = previous_ledger.get("defenses")
    if not isinstance(old, list):
        return current
    for raw in old:
        if not isinstance(raw, Mapping) or not raw.get("id") or raw.get("id") in by_id:
            continue
        item = dict(raw)
        if item.get("state") != "BROKEN":
            confirmed_at = _aware(item.get("first_seen_at"), "defense.first_seen_at")
            price = _positive_number(item.get("price"), "defense.price")
            direction = item.get("direction")
            broken = next(
                (
                    bar
                    for bar in bars
                    if _aware(bar["time"], "bar.time") >= confirmed_at
                    and (
                        float(bar["close"]) < price
                        if direction == "BULL"
                        else float(bar["close"]) > price
                    )
                ),
                None,
            )
            if broken is not None:
                item["state"] = "BROKEN"
                item["broken_at"] = _aware(broken["time"], "bar.time").isoformat()
        by_id[str(item["id"])] = item
    result = list(by_id.values())
    result.sort(key=lambda item: (str(item.get("first_seen_at") or ""), str(item.get("id") or "")))
    return result[-6:]


def _session_bars(bars: Sequence[Mapping[str, Any]], expected: datetime) -> list[dict[str, Any]]:
    start = _monitoring_segment(expected)["start"]
    result: list[dict[str, Any]] = []
    for item in bars:
        if not isinstance(item, Mapping):
            continue
        at = _aware(item.get("time"), "bar.time")
        if start <= at <= expected:
            result.append(dict(item))
    return result


def _ledger_matches_monitoring_segment(
    previous: Mapping[str, Any] | None, expected: datetime
) -> bool:
    if not isinstance(previous, Mapping):
        return False
    recorded = previous.get("monitoring_session")
    if not isinstance(recorded, Mapping) or not recorded.get("start"):
        # Backward-compatible for older deterministic contracts that did not
        # persist an explicit monitoring-segment identity.
        return True
    return str(recorded.get("start")) == _monitoring_segment(expected)["start"].isoformat()


_TAIPEI = ZoneInfo("Asia/Taipei")
_NEW_YORK = ZoneInfo("America/New_York")


def _us_cash_open_taipei(local_trade_date: date) -> datetime:
    """Return 09:30 New York in Taipei time, including DST automatically."""

    return datetime.combine(local_trade_date, time(9, 30), tzinfo=_NEW_YORK).astimezone(_TAIPEI)


def _monitoring_segment(at: datetime) -> dict[str, Any]:
    clock = at.timetz().replace(tzinfo=None)
    if time(8, 45) <= clock <= time(13, 45):
        return {
            "name": "DAY",
            "start": at.replace(hour=8, minute=45, second=0, microsecond=0),
        }

    trade_date = at.date() if clock >= time(15, 0) else (at - timedelta(days=1)).date()
    night_start = datetime.combine(trade_date, time(15, 0), tzinfo=at.tzinfo)
    us_open = _us_cash_open_taipei(trade_date).astimezone(at.tzinfo)
    if at >= us_open or clock < time(5, 0):
        return {"name": "US_OPEN", "start": us_open}
    return {"name": "NIGHT_PM", "start": night_start}


def _reference_monitoring_segment(at: datetime) -> dict[str, Any]:
    active = _monitoring_segment(at)
    name = str(active["name"])
    if name == "DAY":
        prior_date = (at - timedelta(days=1)).date()
        start = _us_cash_open_taipei(prior_date).astimezone(at.tzinfo)
        end = at.replace(hour=5, minute=0, second=0, microsecond=0)
        return {"name": "US_OPEN", "start": start, "end": end}
    if name == "NIGHT_PM":
        start = at.replace(hour=8, minute=45, second=0, microsecond=0)
        end = at.replace(hour=13, minute=45, second=0, microsecond=0)
        return {"name": "DAY", "start": start, "end": end}
    start = active["start"].replace(hour=15, minute=0, second=0, microsecond=0)
    return {"name": "NIGHT_PM", "start": start, "end": active["start"]}


def _bars_between(
    bars: Sequence[Mapping[str, Any]], *, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in bars:
        if not isinstance(item, Mapping):
            continue
        at = _aware(item.get("time"), "bar.time")
        if start <= at < end:
            result.append(dict(item))
    return result


def _monitoring_segment_bars(
    bars: Sequence[Mapping[str, Any]], expected: datetime
) -> list[dict[str, Any]]:
    segment = _monitoring_segment(expected)
    return _bars_between(
        bars,
        start=segment["start"],
        end=expected + timedelta(microseconds=1),
    )


def _session_start(at: datetime) -> datetime | None:
    clock = at.timetz().replace(tzinfo=None)
    if time(8, 45) <= clock <= time(13, 45):
        return at.replace(hour=8, minute=45, second=0, microsecond=0)
    if clock >= time(15, 0):
        return at.replace(hour=15, minute=0, second=0, microsecond=0)
    if clock < time(5, 0):
        prior = at - timedelta(days=1)
        return prior.replace(hour=15, minute=0, second=0, microsecond=0)
    return None


def _ids(value: Mapping[str, Any] | None, key: str) -> set[str]:
    if not isinstance(value, Mapping):
        return set()
    collection = value.get(key)
    if not isinstance(collection, list):
        return set()
    return {str(item.get("id")) for item in collection if isinstance(item, Mapping) and item.get("id")}


def _items_by_id(value: Mapping[str, Any] | None, key: str) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, Mapping) or not isinstance(value.get(key), list):
        return {}
    return {
        str(item.get("id")): item
        for item in value[key]
        if isinstance(item, Mapping) and item.get("id")
    }


def _dow(value: Any) -> str:
    text = str(value or "UNDEFINED")
    return text if text in {"BULL", "BEAR", "TRANSITION", "UNDEFINED"} else "UNDEFINED"


def _id(*parts: str) -> str:
    material = "|".join(parts)
    return f"{parts[0]}-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"


def _aware(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise DeterministicReplayError(f"{field}必須是含時區時間。")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DeterministicReplayError(f"{field}不是有效時間。") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise DeterministicReplayError(f"{field}必須包含時區。")
    return result


def _positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0:
        raise DeterministicReplayError(f"{field}必須是正數。")
    return float(value)
