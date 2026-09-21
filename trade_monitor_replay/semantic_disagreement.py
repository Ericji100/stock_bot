"""Causal AI/program comparison records and read-only post-hoc outcomes.

The deterministic engine is an evidence generator, not the ground truth for
course semantics.  These helpers therefore record differences without choosing
a winner.  Future bars may be attached only by the post-hoc function after the
original AI decision has already been validated and persisted.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence


SEMANTIC_COMPARISON_VERSION = 1
DEFAULT_FORWARD_HORIZONS = (3, 5, 10, 20)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _event_view(value: Any) -> dict[str, Any]:
    event = _mapping(value)
    return {
        key: event.get(key)
        for key in (
            "id",
            "event_type",
            "direction",
            "from_level",
            "to_level",
            "first_seen_at",
            "source_anchor_id",
        )
        if event.get(key) is not None
    }


def _semantic_view(analysis: Mapping[str, Any]) -> dict[str, Any]:
    reading = _mapping(analysis.get("course_reading"))
    action = _mapping(analysis.get("action"))
    scenario = _mapping(analysis.get("scenario"))
    return {
        "large_trend": _mapping(analysis.get("large_trend")).get("classification"),
        "current_trend": _mapping(analysis.get("current_trend")).get("classification"),
        "message_type": analysis.get("message_type"),
        "message_direction": analysis.get("message_direction"),
        "large_anchor_ref": reading.get("large_anchor_ref"),
        "small_anchor_ref": reading.get("small_anchor_ref"),
        "working_anchor_ref": reading.get("working_anchor_ref"),
        "reverse_anchor_candidate_ref": reading.get("reverse_anchor_candidate_ref"),
        "large_defense_ref": reading.get("large_defense_ref"),
        "small_defense_ref": reading.get("small_defense_ref"),
        "structure_event_ref": reading.get("structure_event_ref"),
        "controlling_grade": reading.get("controlling_grade"),
        "grade_relation": reading.get("grade_relation"),
        "background_quadrant": reading.get("background_quadrant"),
        "working_quadrant": reading.get("working_quadrant"),
        "primary_quadrant_candidate": reading.get("primary_quadrant_candidate"),
        "secondary_quadrant_candidate": reading.get("secondary_quadrant_candidate"),
        "background_trend_dynamics": reading.get("background_trend_dynamics"),
        "background_volatility_dynamics": reading.get("background_volatility_dynamics"),
        "working_trend_dynamics": reading.get("working_trend_dynamics"),
        "working_volatility_dynamics": reading.get("working_volatility_dynamics"),
        "cclass_mode": reading.get("cclass_mode"),
        "x_stage": reading.get("x_stage"),
        "focus_methods": list(reading.get("focus_methods") or []),
        "main_strategy": reading.get("main_strategy"),
        "setup_stage": reading.get("setup_stage"),
        "scenario_weights": {
            key: scenario.get(key)
            for key in ("bull_probability", "range_probability", "bear_probability")
            if key in scenario
        },
        "position_action": action.get("position_action"),
        "entry_rejection_reason": action.get("entry_rejection_reason"),
        "setup_key": action.get("setup_key"),
    }


def _program_view(
    ledger: Mapping[str, Any],
    *,
    evidence_events: Sequence[Mapping[str, Any]],
    entry_gate: Mapping[str, Any] | None,
) -> dict[str, Any]:
    anchor_control = _mapping(ledger.get("anchor_control"))
    lifecycle = _mapping(ledger.get("anchor_lifecycle"))
    quadrant = _mapping(lifecycle.get("quadrant_context"))
    dow = _mapping(lifecycle.get("dow_context"))
    methods = _mapping(ledger.get("course_method_state"))
    raw_events = {
        str(item.get("id")): item
        for item in ledger.get("structure_events", [])
        if isinstance(item, Mapping) and item.get("id")
    }
    visible_ids = [
        str(item.get("event_id"))
        for item in evidence_events
        if isinstance(item, Mapping) and item.get("event_id") in raw_events
    ]
    return {
        "authority": "EVIDENCE_CANDIDATES_ONLY",
        "anchor_candidates": {
            key: anchor_control.get(key)
            for key in (
                "active_background_anchor_ref",
                "active_child_anchor_ref",
                "working_leg_ref",
                "reverse_anchor_candidate_ref",
                "large_defense_ref",
                "small_defense_ref",
            )
        },
        "quadrant_evidence": {
            key: quadrant.get(key)
            for key in (
                "authority",
                "reference_anchor_id",
                "reference_grade",
                "anchor_direction",
                "phase",
                "background_primary",
                "background_candidates",
                "background_trend_dynamics",
                "background_volatility_dynamics",
                "working_primary",
                "working_candidates",
                "working_trend_dynamics",
                "working_volatility_dynamics",
            )
        },
        "dow_evidence": {
            key: dow.get(key)
            for key in (
                "large_state",
                "small_state",
                "large_bull_defense",
                "large_bear_defense",
                "small_bull_defense",
                "small_bear_defense",
            )
        },
        "method_evidence": {
            "cclass_mode": methods.get("cclass_mode"),
            "x_stage": methods.get("x_stage"),
            "yizhi": methods.get("yizhi"),
            "left_right": methods.get("left_right"),
        },
        "new_structure_event_candidates": [
            _event_view(raw_events[event_id]) for event_id in visible_ids
        ],
        "entry_candidate": dict(entry_gate or {}),
    }


def _field_difference(
    differences: list[dict[str, Any]],
    *,
    field: str,
    ai_value: Any,
    program_value: Any,
    kind: str,
) -> None:
    if program_value is None or ai_value == program_value:
        return
    differences.append(
        {
            "field": field,
            "kind": kind,
            "ai_value": ai_value,
            "program_candidate": program_value,
            "verdict": "UNRESOLVED",
        }
    )


def build_semantic_comparison(
    *,
    raw_analysis: Mapping[str, Any],
    validated_analysis: Mapping[str, Any],
    ledger: Mapping[str, Any],
    evidence_events: Sequence[Mapping[str, Any]],
    entry_gate: Mapping[str, Any] | None,
    stage: str,
    bar_time: str,
    artifact_key: str,
    analysis_source: str,
) -> dict[str, Any]:
    """Freeze one causal AI/program comparison without post-hoc judgment."""

    raw_ai = _semantic_view(raw_analysis)
    ai = _semantic_view(validated_analysis)
    program = _program_view(
        ledger,
        evidence_events=evidence_events,
        entry_gate=entry_gate,
    )
    candidates = _mapping(program.get("anchor_candidates"))
    quadrant = _mapping(program.get("quadrant_evidence"))
    methods = _mapping(program.get("method_evidence"))
    differences: list[dict[str, Any]] = []

    for ai_key, program_key in (
        ("large_anchor_ref", "active_background_anchor_ref"),
        ("small_anchor_ref", "active_child_anchor_ref"),
        ("working_anchor_ref", "working_leg_ref"),
        ("reverse_anchor_candidate_ref", "reverse_anchor_candidate_ref"),
        ("large_defense_ref", "large_defense_ref"),
        ("small_defense_ref", "small_defense_ref"),
    ):
        _field_difference(
            differences,
            field=ai_key,
            ai_value=ai.get(ai_key),
            program_value=candidates.get(program_key),
            kind="AI_SELECTION_DIFFERS_FROM_PROGRAM_CANDIDATE",
        )
    for key, program_key in (
        ("background_quadrant", "background_primary"),
        ("working_quadrant", "working_primary"),
        ("background_trend_dynamics", "background_trend_dynamics"),
        ("background_volatility_dynamics", "background_volatility_dynamics"),
        ("working_trend_dynamics", "working_trend_dynamics"),
        ("working_volatility_dynamics", "working_volatility_dynamics"),
    ):
        _field_difference(
            differences,
            field=key,
            ai_value=ai.get(key),
            program_value=quadrant.get(program_key),
            kind="AI_INTERPRETATION_DIFFERS_FROM_PROGRAM_BASELINE",
        )
    for key in ("cclass_mode", "x_stage"):
        _field_difference(
            differences,
            field=key,
            ai_value=ai.get(key),
            program_value=methods.get(key),
            kind="AI_METHOD_READING_DIFFERS_FROM_PROGRAM_BASELINE",
        )

    visible_event_ids = {
        item.get("id")
        for item in program.get("new_structure_event_candidates", [])
        if isinstance(item, Mapping)
    }
    selected_event = ai.get("structure_event_ref")
    if visible_event_ids and selected_event not in visible_event_ids:
        differences.append(
            {
                "field": "structure_event_ref",
                "kind": "AI_DID_NOT_SELECT_NEW_PROGRAM_EVENT_CANDIDATE",
                "ai_value": selected_event,
                "program_candidate": sorted(str(item) for item in visible_event_ids if item),
                "verdict": "UNRESOLVED",
            }
        )
    gate = _mapping(program.get("entry_candidate"))
    if gate.get("status") == "ENTRY_ELIGIBLE" and ai.get("position_action") != "ENTER":
        differences.append(
            {
                "field": "position_action",
                "kind": "AI_REJECTED_ENTRY_ELIGIBLE_CANDIDATE",
                "ai_value": ai.get("position_action"),
                "program_candidate": "ENTER",
                "ai_reason": ai.get("entry_rejection_reason"),
                "verdict": "UNRESOLVED",
            }
        )

    normalized_fields = [
        key for key in ai if raw_ai.get(key) != ai.get(key)
    ]
    material = {
        "artifact_key": artifact_key,
        "stage": stage,
        "bar_time": bar_time,
        "analysis_source": analysis_source,
    }
    comparison_id = "SC-" + hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]
    return {
        "version": SEMANTIC_COMPARISON_VERSION,
        "comparison_id": comparison_id,
        "stage": stage,
        "bar_time": bar_time,
        "causal_cutoff": bar_time,
        "analysis_source": analysis_source,
        "artifact_key": artifact_key,
        "ai_raw": raw_ai,
        "ai_validated": ai,
        "validator_changed_semantic_fields": normalized_fields,
        "program_evidence": program,
        "differences": differences,
        "status": "DISAGREEMENT" if differences else "NO_RECORDED_DISAGREEMENT",
        "winner": None,
        "posthoc_outcome": None,
        "note": (
            "本紀錄在當下因果截止點凍結；程式候選不是課程真值。"
            "未來資料只能由盤後工具附加，不得回寫原AI決策。"
        ),
    }


def attach_forward_outcomes(
    comparison: Mapping[str, Any],
    bars: Iterable[Mapping[str, Any]],
    *,
    horizons: Sequence[int] = DEFAULT_FORWARD_HORIZONS,
) -> dict[str, Any]:
    """Attach future price evidence without declaring a semantic winner."""

    cutoff = datetime.fromisoformat(str(comparison["causal_cutoff"]))
    ordered = sorted(
        (
            dict(item)
            for item in bars
            if isinstance(item, Mapping) and item.get("bar_time")
        ),
        key=lambda item: datetime.fromisoformat(str(item["bar_time"])),
    )
    current = next(
        (
            item
            for item in reversed(ordered)
            if datetime.fromisoformat(str(item["bar_time"])) <= cutoff
        ),
        None,
    )
    future = [
        item for item in ordered
        if datetime.fromisoformat(str(item["bar_time"])) > cutoff
    ]
    result = dict(comparison)
    if not isinstance(current, Mapping) or not future:
        result["posthoc_outcome"] = {
            "status": "INSUFFICIENT_FUTURE_BARS",
            "generated_at": datetime.now().astimezone().isoformat(),
            "horizons": {},
        }
        return result

    baseline = float(current["close"])
    outcomes: dict[str, Any] = {}
    for raw_horizon in horizons:
        horizon = int(raw_horizon)
        if horizon <= 0:
            raise ValueError("Forward horizons must be positive.")
        window = future[:horizon]
        if len(window) < horizon:
            outcomes[str(horizon)] = {
                "status": "INSUFFICIENT_BARS",
                "available_bars": len(window),
            }
            continue
        end_close = float(window[-1]["close"])
        max_high = max(float(item["high"]) for item in window)
        min_low = min(float(item["low"]) for item in window)
        outcomes[str(horizon)] = {
            "status": "AVAILABLE",
            "end_bar_time": str(window[-1]["bar_time"]),
            "baseline_close": baseline,
            "end_close": end_close,
            "close_change_points": end_close - baseline,
            "bull_mfe_points": max_high - baseline,
            "bull_mae_points": baseline - min_low,
            "bear_mfe_points": baseline - min_low,
            "bear_mae_points": max_high - baseline,
            "range_points": max_high - min_low,
        }
    result["posthoc_outcome"] = {
        "status": "OUTCOME_ATTACHED_REQUIRES_COURSE_REVIEW",
        "generated_at": datetime.now().astimezone().isoformat(),
        "horizons": outcomes,
        "winner": None,
        "note": "後續漲跌不是課程語意真值；仍須核對當時可見證據與課程定義。",
    }
    return result
