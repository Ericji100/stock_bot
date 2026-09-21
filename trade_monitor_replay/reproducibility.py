"""Program-decision fingerprints for isolated TMF replay runs."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


PROGRAM_EVENT_TYPES = {
    "PROGRAM_SETUP_ARMED",
    "PROGRAM_SETUP_CHANGED",
    "PROGRAM_DEFENSE_AVAILABLE",
    "PROGRAM_QUADRANT_CHANGED",
    "PROGRAM_TAIJI_CHANGED",
    "PROGRAM_METHOD_CHANGED",
    "PROGRAM_NUMBERED_MARKET_CHANGED",
}
EXECUTION_EVENT_TYPES = {
    "ENTRY_FILLED",
    "ENTRY_EXPIRED",
    "ENTRY_REJECTED_GAP",
    "STOP_PROTECTION_MOVED",
    "STOP_FILLED",
    "EXIT_FILLED",
    "PROGRAM_SETUP_INVALIDATED",
    "PROGRAM_CONSTITUTION_LOCKED",
    "PROGRAM_CONSTITUTION_REQUALIFIED",
}
STRICT_V34_HASH_FIELDS = (
    "rule_sha256",
    "model_rules_sha256",
    "schema_sha256",
    "execution_prompt_sha256",
    "execution_implementation_sha256",
    "deterministic_timeline_engine_sha256",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
HARD_FACT_ARTIFACTS = (
    "bars.csv",
    "structured-market-data.json",
    "deterministic-event-state.json",
    "deterministic-evidence-ledger.json",
    "deterministic-timeline.json",
)


def build_program_fingerprint(
    run_dir: Path,
    *,
    require_complete: bool = False,
) -> dict[str, Any]:
    """Hash trading facts while deliberately excluding model prose."""

    manifest = _json(run_dir / "manifest.json")
    planned_selected = [str(item) for item in manifest.get("selected_bar_times", [])]
    completed = int(manifest.get("next_day_index") or 0)
    strict_errors = _strict_manifest_errors(manifest)
    if require_complete and strict_errors:
        raise ValueError("; ".join(strict_errors))
    selected = planned_selected[: max(0, min(completed, len(planned_selected)))]
    actions: list[dict[str, Any]] = []
    for at in selected:
        key = _bar_key(at)
        validated = _latest(run_dir / "analysis", f"{key}*-validated.json")
        checkpoint = _latest(run_dir / "analysis", f"{key}*-state-checkpoint.json")
        if validated is None or checkpoint is None:
            raise ValueError(f"Missing canonical replay artifacts for {at}.")
        analysis = _json(validated).get("analysis")
        action = analysis.get("action") if isinstance(analysis, Mapping) else None
        reading = analysis.get("course_reading") if isinstance(analysis, Mapping) else None
        scenario = analysis.get("scenario") if isinstance(analysis, Mapping) else None
        checkpoint_payload = _json(checkpoint)
        state = checkpoint_payload.get("position_state")
        constitution = checkpoint_payload.get("constitution_state")
        checkpoint_ledger = checkpoint_payload.get("deterministic_evidence_ledger")
        checkpoint_methods = (
            checkpoint_ledger.get("course_method_state")
            if isinstance(checkpoint_ledger, Mapping)
            else None
        )
        actions.append(
            {
                "bar_time": at,
                "course_state": _pick(
                    reading,
                    "large_anchor_ref",
                    "small_anchor_ref",
                    "working_anchor_ref",
                    "controlling_grade",
                    "grade_relation",
                    "background_quadrant",
                    "working_quadrant",
                    "primary_quadrant_candidate",
                    "secondary_quadrant_candidate",
                    "background_trend_dynamics",
                    "background_volatility_dynamics",
                    "working_trend_dynamics",
                    "working_volatility_dynamics",
                    "main_strategy_family",
                    "focus_methods",
                    "cclass_mode",
                    "x_stage",
                    "setup_stage",
                ),
                "scenario_weights": _pick(
                    scenario,
                    "bull_probability",
                    "range_probability",
                    "bear_probability",
                ),
                "course_assessment": _canonical_ai_course_assessment(
                    analysis.get("ai_course_assessment")
                    if isinstance(analysis, Mapping)
                    else None
                ),
                "action": _pick(
                    action,
                    "position_action",
                    "direction",
                    "entry_role",
                    "setup_key",
                    "stop_price",
                    "max_wait_bars",
                    "entry_rejection_reason",
                ),
                "position": _canonical_position(state),
                "entry_eligibility": _canonical_entry_eligibility(
                    checkpoint_payload.get("entry_eligibility")
                ),
                "constitution": _canonical_constitution(constitution),
                "numbered_market": _canonical_numbered_market(
                    checkpoint_methods.get("numbered_market")
                    if isinstance(checkpoint_methods, Mapping)
                    else None
                ),
                "program_method_scan": (
                    checkpoint_methods.get("method_scan")
                    if isinstance(checkpoint_methods, Mapping)
                    else None
                ),
            }
        )

    timeline_payload = _json_optional(run_dir / "deterministic-timeline.json") or {}
    program_timeline: list[dict[str, Any]] = []
    for entry in timeline_payload.get("event_times", []):
        if not isinstance(entry, Mapping):
            continue
        for event in entry.get("events", []):
            if isinstance(event, Mapping) and event.get("event_type") in PROGRAM_EVENT_TYPES:
                program_timeline.append(_without_runtime_fields(event))

    execution_events = [
        _canonical_execution_event(
            {
                **_without_runtime_fields(item),
                "event_at": item.get("fill_time") or item.get("recorded_at"),
            }
        )
        for item in _jsonl(run_dir / "execution-events.jsonl")
        if item.get("event_type") in EXECUTION_EVENT_TYPES
    ]
    payload = {
        "version": 5,
        "input_identity": {
            "target_date": manifest.get("target_date"),
            "instrument": manifest.get("instrument"),
            "expiry_month": manifest.get("expiry_month"),
            "source_sha256": manifest.get("source_sha256"),
            "rule_version": manifest.get("rule_version"),
            "rule_sha256": manifest.get("rule_sha256"),
            "model_rules_sha256": manifest.get("model_rules_sha256"),
            "schema_sha256": manifest.get("schema_sha256"),
            "execution_version": manifest.get("execution_version"),
            "execution_prompt_sha256": manifest.get("execution_prompt_sha256"),
            "execution_implementation_sha256": manifest.get(
                "execution_implementation_sha256"
            ),
            "analysis_mode": manifest.get("analysis_mode"),
            "decision_authority": manifest.get("decision_authority"),
            "execution_profile": manifest.get("execution_profile"),
            "ai_input_view_version": manifest.get("ai_input_view_version"),
            "trade_direction_policy": manifest.get("trade_direction_policy", "BOTH"),
            "trade_setup_policy": manifest.get("trade_setup_policy", "ALL"),
            "ai_provider": manifest.get("ai_provider"),
            "ai_model": manifest.get("ai_model"),
            "ai_reasoning_effort": manifest.get("ai_reasoning_effort"),
            "deterministic_timeline_engine_version": manifest.get(
                "deterministic_timeline_engine_version"
            ),
            "deterministic_timeline_engine_sha256": manifest.get(
                "deterministic_timeline_engine_sha256"
            ),
            "selected_bar_times": planned_selected,
            "presentation_bar_times": [
                str(item) for item in manifest.get("presentation_bar_times", [])
            ],
            "mode": manifest.get("mode"),
            "start_time": manifest.get("start_time"),
            "end_time": manifest.get("end_time"),
            "analysis_times": manifest.get("analysis_times"),
            "max_bars": manifest.get("max_bars"),
            "analysis_every_bars": manifest.get("analysis_every_bars"),
            "detail_bar_count": manifest.get("detail_bar_count"),
            "event_driven": manifest.get("event_driven"),
            "deterministic_timeline_mode": manifest.get("deterministic_timeline_mode"),
            "preopen_only": manifest.get("preopen_only"),
            "program_tick_every_bars": manifest.get("program_tick_every_bars"),
            "interval_seconds": manifest.get("interval_seconds"),
            "program_only": manifest.get("program_only"),
            "review_mode": manifest.get("review_mode"),
            "human_intervention": manifest.get("human_intervention"),
        },
        "program_timeline": program_timeline,
        "actions_and_positions": actions,
        "execution_events": execution_events,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "ok": True,
        "run_id": manifest.get("run_id") or run_dir.name,
        "complete": not strict_errors,
        "completion_errors": strict_errors,
        "completed_analysis_count": completed,
        "program_fingerprint_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "payload": payload,
    }


def compare_program_fingerprints(left: Path, right: Path) -> dict[str, Any]:
    try:
        first = build_program_fingerprint(left, require_complete=True)
        second = build_program_fingerprint(right, require_complete=True)
    except ValueError as exc:
        return {
            "ok": False,
            "status": "incomplete_or_invalid",
            "error": str(exc),
        }
    same = first["program_fingerprint_sha256"] == second["program_fingerprint_sha256"]
    return {
        "ok": same,
        "status": "identical" if same else "different",
        "left_run_id": first["run_id"],
        "right_run_id": second["run_id"],
        "left_sha256": first["program_fingerprint_sha256"],
        "right_sha256": second["program_fingerprint_sha256"],
        "left_complete": first["complete"],
        "right_complete": second["complete"],
    }


def build_execution_decision_trace(
    run_dir: Path,
    *,
    require_complete: bool = True,
) -> dict[str, Any]:
    """Build the exact causal ENTER/HOLD/EXIT/WAIT trace for acceptance.

    Prose is excluded, while course family, entry gate, stops, pending/fill
    state and canonical execution events remain.  This makes model wording
    variation harmless without hiding a materially different trade decision.
    """

    fingerprint = build_program_fingerprint(
        run_dir,
        require_complete=require_complete,
    )
    execution_events = fingerprint["payload"]["execution_events"]
    execution_by_time: dict[str, list[dict[str, Any]]] = {}
    accepted_family_by_setup: dict[str, str] = {}
    for event in execution_events:
        if event.get("event_type") == "ENTRY_FILLED":
            setup_key = str(event.get("setup_key") or "")
            family = str(event.get("main_strategy_family") or "")
            if setup_key and family in {"Q2", "Q4"}:
                accepted_family_by_setup[setup_key] = family
        event_time = next(
            (
                str(event.get(field))
                for field in ("event_at", "fill_time", "trigger_time")
                if event.get(field)
            ),
            None,
        )
        if event_time:
            execution_by_time.setdefault(event_time, []).append(dict(event))

    trace: list[dict[str, Any]] = []
    course_trace: list[dict[str, Any]] = []
    for row in fingerprint["payload"]["actions_and_positions"]:
        action = row.get("action") if isinstance(row, Mapping) else None
        position = row.get("position") if isinstance(row, Mapping) else None
        bar_time = str(row.get("bar_time") or "")
        position_action = str(
            action.get("position_action") if isinstance(action, Mapping) else "NONE"
        ).upper()
        position_status = str(
            position.get("status") if isinstance(position, Mapping) else "FLAT"
        ).upper()
        if position_action == "ENTER":
            decision = "ENTER"
        elif position_action == "EXIT":
            decision = "EXIT"
        elif position_status in {"LONG", "SHORT"}:
            decision = "HOLD"
        else:
            decision = "WAIT"
        events = execution_by_time.get(bar_time, [])
        exit_kind = (
            "STOP"
            if any(item.get("event_type") == "STOP_FILLED" for item in events)
            else "ACTIVE_EXIT"
            if any(item.get("event_type") == "EXIT_FILLED" for item in events)
            else None
        )
        main_strategy_family = (
            row.get("course_state", {}).get("main_strategy_family")
            if isinstance(row.get("course_state"), Mapping)
            else None
        )
        # After entry, the accepted setup family is an execution fact stored on
        # ENTRY_FILLED.  A later HOLD/STOP/EXIT explanation may legitimately
        # omit or restate that label; it must not change the trade's family in
        # the reproducibility trace.
        if decision != "ENTER":
            setup_key = str(
                (position.get("active_setup_key") if isinstance(position, Mapping) else None)
                or (action.get("setup_key") if isinstance(action, Mapping) else None)
                or ""
            )
            if setup_key in accepted_family_by_setup:
                main_strategy_family = accepted_family_by_setup[setup_key]
        # A flat WAIT with no gate, pending fill, or execution event may still
        # mention Q2/Q4 as the setup currently being observed.  That is course
        # interpretation and belongs in ``course_trace``; it is not an
        # ENTER/HOLD/EXIT decision.  Keep the family strict whenever an actual
        # trade or execution event exists.
        if decision == "WAIT" and exit_kind is None and not events:
            main_strategy_family = None
        trace.append(
            {
                "bar_time": bar_time,
                "decision": decision,
                "exit_kind": exit_kind,
                "main_strategy_family": main_strategy_family,
                "action": _canonical_trade_decision_action(
                    action,
                    entry_eligibility=row.get("entry_eligibility"),
                    position=position,
                ),
                "entry_eligibility": row.get("entry_eligibility"),
                "position": position,
                "execution_events": events,
            }
        )
        course_trace.append(
            {
                "bar_time": bar_time,
                "course_state": row.get("course_state"),
                "scenario_weights": row.get("scenario_weights"),
                "course_assessment": row.get("course_assessment"),
            }
        )
    hard_fact_trace = {
        "artifact_sha256": {
            name: _file_sha256_optional(run_dir / name)
            for name in HARD_FACT_ARTIFACTS
        },
        "program_timeline": fingerprint["payload"]["program_timeline"],
        "entry_eligibility": [
            {
                "bar_time": row.get("bar_time"),
                "entry_eligibility": row.get("entry_eligibility"),
            }
            for row in fingerprint["payload"]["actions_and_positions"]
        ],
    }
    canonical = json.dumps(
        trace,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    course_canonical = json.dumps(
        course_trace,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    hard_fact_canonical = json.dumps(
        hard_fact_trace,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "ok": True,
        "run_id": fingerprint["run_id"],
        "input_identity": fingerprint["payload"]["input_identity"],
        "decision_trace_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "course_state_trace_sha256": hashlib.sha256(
            course_canonical.encode("utf-8")
        ).hexdigest(),
        "hard_fact_trace_sha256": hashlib.sha256(
            hard_fact_canonical.encode("utf-8")
        ).hexdigest(),
        "trace": trace,
        "course_trace": course_trace,
        "hard_fact_trace": hard_fact_trace,
    }


def _canonical_trade_decision_action(
    action: Any,
    *,
    entry_eligibility: Any,
    position: Any,
) -> dict[str, Any]:
    """Exclude non-executable setup labels from the trade-decision trace.

    A model may name the setup it is merely discussing while the account is
    flat and the deterministic gate has no actionable event.  That label is
    course interpretation, not an ENTER/HOLD/EXIT decision.  Once a setup is
    entry-eligible, queued, held, or attached to an execution action, its key
    and risk fields remain strict acceptance facts.
    """

    selected = dict(action) if isinstance(action, Mapping) else {}
    position_action = str(selected.get("position_action") or "NONE").upper()
    position_status = str(
        position.get("status") if isinstance(position, Mapping) else "FLAT"
    ).upper()
    eligibility_status = str(
        entry_eligibility.get("status")
        if isinstance(entry_eligibility, Mapping)
        else "NONE"
    ).upper()
    pending_entry = (
        position.get("pending_entry") if isinstance(position, Mapping) else None
    )
    executable_context = bool(
        position_action in {"ENTER", "EXIT", "STOP"}
        or position_status in {"LONG", "SHORT"}
        or eligibility_status == "ENTRY_ELIGIBLE"
        or (
            isinstance(pending_entry, Mapping)
            and bool(pending_entry.get("setup_key"))
        )
    )
    if executable_context:
        # ``max_wait_bars`` becomes an executable fact only when ENTER queues
        # the setup and the value is copied into pending/position behavior
        # state.  Repeating or omitting it on later HOLD/EXIT/STOP cards does
        # not mutate that accepted plan, so treating the prose echo as a fresh
        # trade decision creates false reproducibility failures.
        if position_action != "ENTER":
            selected.pop("max_wait_bars", None)
        return selected

    # Flat WAIT has no legal trade attached to the model's explanatory setup
    # choice.  Keep only the actual no-action decision; setup-specific risk and
    # rejection fields remain in the separate course-state audit.
    return _pick(selected, "position_action", "direction")


def verify_triplicate_runs(run_dirs: Sequence[Path]) -> dict[str, Any]:
    """Fail closed unless exactly three complete, identical-spec traces agree."""

    if len(run_dirs) != 3:
        return {
            "ok": False,
            "status": "invalid_run_count",
            "required_run_count": 3,
            "actual_run_count": len(run_dirs),
        }
    try:
        traces = [build_execution_decision_trace(path) for path in run_dirs]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "status": "incomplete_or_invalid",
            "error": str(exc),
        }
    run_ids = [str(item["run_id"]) for item in traces]
    if len(set(run_ids)) != 3:
        return {
            "ok": False,
            "status": "duplicate_run",
            "run_ids": run_ids,
        }
    identities = [
        json.dumps(
            item["input_identity"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for item in traces
    ]
    if len(set(identities)) != 1:
        return {
            "ok": False,
            "status": "different_input_or_execution_spec",
            "run_ids": run_ids,
        }
    hard_fact_hashes = [str(item["hard_fact_trace_sha256"]) for item in traces]
    if len(set(hard_fact_hashes)) != 1:
        first_difference = _first_hard_fact_difference(
            traces[0]["hard_fact_trace"],
            traces[1]["hard_fact_trace"],
        )
        if first_difference is None:
            first_difference = _first_hard_fact_difference(
                traces[0]["hard_fact_trace"],
                traces[2]["hard_fact_trace"],
            )
        return {
            "ok": False,
            "status": "hard_fact_trace_mismatch",
            "run_ids": run_ids,
            "hard_fact_trace_sha256": hard_fact_hashes,
            "first_difference": first_difference,
        }
    hashes = [str(item["decision_trace_sha256"]) for item in traces]
    if len(set(hashes)) != 1:
        first_difference = _first_trace_difference(
            traces[0]["trace"],
            traces[1]["trace"],
        )
        if first_difference is None:
            first_difference = _first_trace_difference(
                traces[0]["trace"],
                traces[2]["trace"],
            )
        return {
            "ok": False,
            "status": "decision_trace_mismatch",
            "run_ids": run_ids,
            "decision_trace_sha256": hashes,
            "first_difference": first_difference,
        }
    course_hashes = [str(item["course_state_trace_sha256"]) for item in traces]
    course_same = len(set(course_hashes)) == 1
    first_course_difference = None
    if not course_same:
        first_course_difference = _first_trace_difference(
            traces[0]["course_trace"],
            traces[1]["course_trace"],
        )
        if first_course_difference is None:
            first_course_difference = _first_trace_difference(
                traces[0]["course_trace"],
                traces[2]["course_trace"],
            )
    return {
        "ok": True,
        "status": (
            "triplicate_identical"
            if course_same
            else "triplicate_trade_decisions_identical_course_state_varied"
        ),
        "run_ids": run_ids,
        "decision_trace_sha256": hashes[0],
        "hard_fact_consistent": True,
        "hard_fact_trace_sha256": hard_fact_hashes[0],
        "course_state_consistent": course_same,
        "course_state_trace_sha256": (
            course_hashes[0] if course_same else course_hashes
        ),
        "first_course_difference": first_course_difference,
    }


def _canonical_execution_event(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep execution facts/codes while excluding generated prose.

    Trigger descriptions, expected-behavior sentences and obstacle labels are
    presentation text.  They must not make two otherwise identical fills look
    like different execution decisions.  Prices, roles, identifiers, timing,
    policies and reason codes remain part of the fail-closed trace.
    """

    result = _pick(
        value,
        "event_type",
        "event_id",
        "setup_key",
        "direction",
        "entry_role",
        "signal_time",
        "trigger_time",
        "fill_time",
        "fill_price",
        "entry_time",
        "entry_price",
        "stop_price",
        "previous_stop_price",
        "new_stop_price",
        "expires_at",
        "invalidated_at",
        "behavior_max_wait_bars",
        "behavior_trigger_level",
        "behavior_policy",
        "entry_strategy",
        "candidate_source",
        "facts_hash",
        "main_strategy_family",
        "reason_code",
        "event_at",
    )
    obstacles = value.get("behavior_obstacles")
    if isinstance(obstacles, list):
        result["behavior_obstacles"] = [
            _pick(item, "role", "price")
            for item in obstacles
            if isinstance(item, Mapping)
        ]
    return result


def _canonical_ai_course_assessment(value: Any) -> dict[str, Any]:
    selected = _pick(
        value,
        "setup_key",
        "facts_hash",
        "overall",
        "checks",
        "evidence_refs",
        "reason_codes",
    )
    return selected if isinstance(value, Mapping) else {}


def _strict_manifest_errors(manifest: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    selected = manifest.get("selected_bar_times")
    selected_count = len(selected) if isinstance(selected, list) else 0
    try:
        completed = int(manifest.get("next_day_index") or 0)
    except (TypeError, ValueError):
        completed = -1
    if manifest.get("status") != "completed":
        errors.append("manifest.status必須為completed")
    if selected_count <= 0 or completed != selected_count:
        errors.append("next_day_index必須等於完整selected_bar_times數量且不得為空")
    if manifest.get("execution_version_migrations"):
        errors.append("一致性驗收不得包含execution_version_migrations")

    is_v34 = (
        manifest.get("analysis_mode") == "AI_HYBRID"
        and manifest.get("trade_direction_policy") == "LONG_ONLY"
        and manifest.get("trade_setup_policy") == "LONG_Q2_Q4_ONLY"
    )
    if is_v34:
        if manifest.get("preopen_completed") is not True:
            errors.append("v34完整回放必須完成盤前快照")
        for field in STRICT_V34_HASH_FIELDS:
            value = manifest.get(field)
            if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
                errors.append(f"v34缺少合法{field}")
        if not manifest.get("deterministic_timeline_engine_version"):
            errors.append("v34缺少deterministic_timeline_engine_version")
        for field in ("ai_provider", "ai_model", "ai_reasoning_effort"):
            if not manifest.get(field):
                errors.append(f"v34缺少{field}")
    return errors


def _first_trace_difference(
    left: Sequence[Mapping[str, Any]],
    right: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    limit = max(len(left), len(right))
    for index in range(limit):
        first = left[index] if index < len(left) else None
        second = right[index] if index < len(right) else None
        if first != second:
            return {
                "index": index,
                "bar_time": (
                    first.get("bar_time")
                    if isinstance(first, Mapping)
                    else second.get("bar_time")
                    if isinstance(second, Mapping)
                    else None
                ),
                "left": first,
                "right": second,
            }
    return None


def _first_hard_fact_difference(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> dict[str, Any] | None:
    for field in ("artifact_sha256", "program_timeline", "entry_eligibility"):
        first = left.get(field)
        second = right.get(field)
        if first == second:
            continue
        if isinstance(first, list) and isinstance(second, list):
            detail = _first_trace_difference(first, second)
        else:
            detail = {"left": first, "right": second}
        return {"field": field, "detail": detail}
    return None


def _canonical_entry_eligibility(value: Any) -> dict[str, Any]:
    return _pick(
        value,
        "status",
        "event_id",
        "setup_key",
        "signal_time",
        "eligible_from",
        "expires_at",
        "decision_authority",
        "required_candidate_source",
        "required_entry_strategy",
        "required_facts_hash",
        "required_stop_price",
        "required_entry_rejection_reason",
    )


def _canonical_position(value: Any) -> dict[str, Any]:
    selected = _pick(
        value,
        "status",
        "entry_time",
        "entry_price",
        "stop_price",
        "direction",
        "active_setup_key",
        "last_stop_time",
        "reentry_count",
        "last_action",
    )
    if isinstance(value, Mapping):
        selected["pending_entry"] = _pick(
            value.get("pending_entry"),
            "event_id",
            "setup_key",
            "direction",
            "entry_role",
            "signal_time",
            "eligible_from",
            "expires_at",
            "stop_price",
            "behavior_max_wait_bars",
        )
    return selected


def _canonical_numbered_market(value: Any) -> dict[str, Any]:
    selected = _pick(
        value,
        "status",
        "number",
        "label",
        "direction",
        "first_direction",
        "first_confirmed_at",
        "last_changed_at",
        "source_anchor_ref",
        "restriction",
        "active_defense_ref",
        "active_defense_time",
        "active_defense_price",
    )
    if isinstance(value, Mapping):
        selected["transitions"] = value.get("transitions")
        selected["consumed_takeover_anchor_refs"] = value.get(
            "consumed_takeover_anchor_refs"
        )
        selected["pending_flip"] = value.get("pending_flip")
    return selected


def _canonical_constitution(value: Any) -> dict[str, Any]:
    selected = _pick(
        value,
        "trading_day",
        "simulated_entry_count",
        "consecutive_simulated_stops",
        "cooldown_until",
        "requalification_required",
    )
    if isinstance(value, Mapping):
        selected["simulated_position"] = _pick(
            value.get("simulated_position"),
            "position_id",
            "setup_id",
            "direction",
            "entry_price_estimate",
            "stop_price_estimate",
            "risk_points",
            "opened_at",
        )
        selected["setup_entry_counts"] = dict(value.get("setup_entry_counts") or {})
    return selected


def _pick(value: Any, *keys: str) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    return {key: source.get(key) for key in keys}


def _without_runtime_fields(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): item
        for key, item in value.items()
        if key not in {"recorded_at"}
    }


def _file_sha256_optional(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bar_key(value: str) -> str:
    from datetime import datetime

    return "day-" + datetime.fromisoformat(value).strftime("%Y%m%d-%H%M")


def _latest(directory: Path, pattern: str) -> Path | None:
    candidates = list(directory.glob(pattern))
    return max(candidates, key=lambda path: path.stat().st_mtime_ns) if candidates else None


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _json_optional(path: Path) -> dict[str, Any] | None:
    return _json(path) if path.exists() else None


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    result: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if isinstance(value, dict):
            result.append(value)
    return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", type=Path, nargs="+")
    args = parser.parse_args()
    if len(args.runs) == 1:
        result = build_program_fingerprint(args.runs[0])
    elif len(args.runs) == 2:
        result = compare_program_fingerprints(args.runs[0], args.runs[1])
    elif len(args.runs) == 3:
        result = verify_triplicate_runs(args.runs)
    else:
        result = {
            "ok": False,
            "status": "invalid_run_count",
            "allowed_run_counts": [1, 2, 3],
            "actual_run_count": len(args.runs),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
