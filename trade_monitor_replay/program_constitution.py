"""Deterministic trading-constitution adapter for isolated replay runs.

The formal monitor already owns the durable constitution state machine.  The
replay engine must not ask a language model to recreate those transitions, so
this module translates program execution fills into the formal event format
and applies a deliberately strict, course-grounded requalification gate.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from trade_monitor.constitution_state import (
    apply_constitution_event,
    load_constitution_snapshot,
)


PROGRAM_CONSTITUTION_AUTHORITY = "PROGRAM_FORMAL_CONSTITUTION_V1"
EXECUTION_TO_CONSTITUTION = {
    "ENTRY_FILLED": "SIM_ENTER",
    "STOP_FILLED": "SIM_STOP",
    "EXIT_FILLED": "SIM_EXIT",
}


class ProgramConstitutionError(RuntimeError):
    pass


def apply_program_execution_event(
    path: Path,
    execution_event: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Apply one canonical program fill and return any new lock event."""

    execution_type = str(execution_event.get("event_type") or "")
    constitution_type = EXECUTION_TO_CONSTITUTION.get(execution_type)
    if constitution_type is None:
        at = _event_time(execution_event)
        return load_constitution_snapshot(path, at=at), None
    realized_points = _execution_realized_points(execution_event)
    if (
        execution_type == "STOP_FILLED"
        and realized_points is not None
        and realized_points > 0
    ):
        # The order was a resting protective stop, but the constitution's
        # consecutive-stop counter represents failed/loss trades.  A trailing
        # stop filled favorably must close the position without consuming one
        # of those loss slots.  Preserve STOP_FILLED in the immutable execution
        # log; only its constitution transition is normalized to SIM_EXIT.
        constitution_type = "SIM_EXIT"

    at = _event_time(execution_event)
    before = load_constitution_snapshot(path, at=at)
    event_id = _required_text(execution_event.get("event_id"), "execution event_id")
    setup_key = _optional_text(execution_event.get("setup_key"))
    position_before = before.get("simulated_position")
    position_id: str | None
    if constitution_type == "SIM_ENTER":
        if setup_key is None:
            raise ProgramConstitutionError("ENTRY_FILLED缺少setup_key。")
        position_id = _stable_id("POSITION", event_id, setup_key)
        entry = _positive_number(execution_event.get("fill_price"), "fill_price")
        stop = _positive_number(execution_event.get("stop_price"), "stop_price")
        risk = abs(entry - stop)
        if risk <= 0:
            raise ProgramConstitutionError("ENTRY_FILLED的結構風險必須大於0。")
        direction = _required_text(execution_event.get("direction"), "direction")
        reason = "程式進場訊號於下一根可成交1分K開盤成交。"
    else:
        if not isinstance(position_before, Mapping):
            raise ProgramConstitutionError(
                f"{execution_type}沒有可對應的回放交易憲法持倉。"
            )
        position_id = _required_text(position_before.get("position_id"), "position_id")
        setup_key = _optional_text(position_before.get("setup_id")) or setup_key
        direction = _optional_text(execution_event.get("direction"))
        entry = None
        stop = None
        risk = None
        reason = (
            "程式獲利保護停損已成交。"
            if execution_type == "STOP_FILLED" and constitution_type == "SIM_EXIT"
            else "程式結構停損已成交。"
            if constitution_type == "SIM_STOP"
            else "程式時間／動機失效出場已成交。"
        )

    normalized = {
        "event_type": constitution_type,
        "event_id": event_id,
        "setup_id": setup_key,
        "position_id": position_id,
        "direction": direction,
        "entry_price_estimate": entry,
        "stop_price_estimate": stop,
        "risk_points": risk,
        "latest_closed_bar_time": at.isoformat(),
        "reason": reason,
    }
    result = apply_constitution_event(
        path,
        normalized,
        expected_latest_closed_bar_time=at.isoformat(),
    )
    after = result.get("state")
    if not isinstance(after, dict):
        raise ProgramConstitutionError("回放交易憲法沒有回傳有效狀態。")

    lock_event = None
    if (
        constitution_type == "SIM_STOP"
        and before.get("trading_locked") is not True
        and after.get("trading_locked") is True
        and int(after.get("consecutive_simulated_stops") or 0) >= 3
    ):
        lock_event = {
            "event_type": "PROGRAM_CONSTITUTION_LOCKED",
            "event_id": _stable_id("PROGRAM_CONSTITUTION_LOCKED", event_id),
            "source_event_id": event_id,
            "locked_at": at.isoformat(),
            "cooldown_until": after.get("cooldown_until"),
            "consecutive_simulated_stops": after.get("consecutive_simulated_stops"),
            "requalification_required": True,
            "authority": PROGRAM_CONSTITUTION_AUTHORITY,
        }
    return after, lock_event


def requalification_audit(
    snapshot: Mapping[str, Any],
    ledger: Mapping[str, Any],
    *,
    as_of: str,
) -> dict[str, Any]:
    """Require a fresh, fully aligned course structure after the cooldown.

    Merely reaching the 30-minute timestamp is intentionally insufficient.
    A new executable candidate must be born after the cooldown began and agree
    with a newly active large anchor, both Dow grades, and a classified working
    quadrant.  This turns the formal prose requirement into auditable facts.
    """

    now = _aware(as_of)
    required = snapshot.get("requalification_required") is True
    result: dict[str, Any] = {
        "version": 1,
        "status": "NOT_REQUIRED" if not required else "WAITING",
        "as_of": now.isoformat(),
        "authority": PROGRAM_CONSTITUTION_AUTHORITY,
        "eligible": False,
        "reason_codes": [],
        "evidence": {},
    }
    if not required:
        return result
    if snapshot.get("cooldown_active") is True:
        result["reason_codes"] = ["COOLDOWN_ACTIVE"]
        return result
    if snapshot.get("simulated_position") is not None:
        result["reason_codes"] = ["POSITION_STILL_OPEN"]
        return result

    lifecycle = ledger.get("anchor_lifecycle")
    lifecycle = lifecycle if isinstance(lifecycle, Mapping) else {}
    background = lifecycle.get("background_anchor")
    background = background if isinstance(background, Mapping) else {}
    dow = lifecycle.get("dow_context")
    dow = dow if isinstance(dow, Mapping) else {}
    quadrant = lifecycle.get("quadrant_context")
    quadrant = quadrant if isinstance(quadrant, Mapping) else {}
    methods = ledger.get("course_method_state")
    methods = methods if isinstance(methods, Mapping) else {}
    levels = ledger.get("trade_levels")
    levels = levels if isinstance(levels, Mapping) else {}
    candidate = levels.get("continuation_arm_candidate")
    candidate = candidate if isinstance(candidate, Mapping) else {}

    trade_direction = str(candidate.get("direction") or "")
    course_direction = {"LONG": "BULL", "SHORT": "BEAR"}.get(trade_direction)
    background_direction = str(background.get("direction") or "")
    large_dow = str(dow.get("large_state") or "UNDEFINED")
    small_dow = str(dow.get("small_state") or "UNDEFINED")
    working_quadrant = str(quadrant.get("working_primary") or "UNDEFINED")
    cclass_mode = str(methods.get("cclass_mode") or "UNDEFINED")
    numbered = methods.get("numbered_market")
    numbered = numbered if isinstance(numbered, Mapping) else {}
    numbered_direction = str(numbered.get("direction") or "")
    numbered_value = numbered.get("number")
    candidate_first_seen = _optional_aware(candidate.get("first_seen_at"))
    cooldown_until = _optional_aware(snapshot.get("cooldown_until"))
    candidate_fresh = (
        candidate_first_seen is not None
        and cooldown_until is not None
        and candidate_first_seen >= cooldown_until
    )
    background_fresh = (
        _optional_aware(background.get("first_seen_at")) is not None
        and cooldown_until is not None
        and _aware(str(background.get("first_seen_at"))) >= cooldown_until
    )

    checks = {
        "market_readable": isinstance(ledger.get("latest_closed_k"), Mapping),
        "candidate_executable": candidate.get("course_entry_quality") == "EXECUTABLE",
        "candidate_fresh_after_cooldown": candidate_fresh,
        "large_anchor_active": (
            background.get("status") == "ACTIVE"
            and background.get("level") == "LARGE"
            and background_direction == course_direction
        ),
        "large_anchor_fresh_after_cooldown": background_fresh,
        "large_dow_aligned": large_dow == course_direction,
        "small_dow_aligned": small_dow == course_direction,
        "quadrant_classified": working_quadrant in {"Q1", "Q2", "Q3", "Q4"},
        "course_mode_classified": cclass_mode in {"TAIJI_ORDERED", "YIZHI_MOMENTUM"},
        "numbered_market_reestablished": (
            numbered.get("status") == "NUMBERED"
            and numbered_direction == course_direction
            and isinstance(numbered_value, int)
            and (
                numbered_value < 2
                or candidate.get("candidate_source") == "FALSE_BREAK_RECLAIM"
            )
        ),
    }
    result["evidence"] = {
        **checks,
        "candidate_setup_key": candidate.get("setup_key"),
        "candidate_direction": trade_direction or None,
        "candidate_first_seen_at": (
            None if candidate_first_seen is None else candidate_first_seen.isoformat()
        ),
        "large_anchor_ref": background.get("id"),
        "large_anchor_direction": background_direction or None,
        "large_anchor_first_seen_at": background.get("first_seen_at"),
        "large_dow": large_dow,
        "small_dow": small_dow,
        "working_quadrant": working_quadrant,
        "cclass_mode": cclass_mode,
        "numbered_market": numbered.get("label"),
        "numbered_market_direction": numbered_direction or None,
    }
    failed = [name for name, passed in checks.items() if passed is not True]
    if failed:
        result["reason_codes"] = [f"REQUALIFICATION_{name.upper()}_MISSING" for name in failed]
        return result
    result.update(
        {
            "status": "QUALIFIED",
            "eligible": True,
            "reason_codes": ["FRESH_FULL_COURSE_STRUCTURE_REQUALIFIED"],
        }
    )
    return result


def apply_program_requalification(
    path: Path,
    audit: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if audit.get("eligible") is not True or audit.get("status") != "QUALIFIED":
        raise ProgramConstitutionError("未通過完整課程結構重認證，不得解除交易憲法鎖定。")
    at = _aware(str(audit.get("as_of") or ""))
    evidence = audit.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    event_id = _stable_id(
        "COOLDOWN_REQUALIFIED",
        at.isoformat(),
        evidence.get("large_anchor_ref"),
        evidence.get("candidate_setup_key"),
    )
    event = {
        "event_type": "COOLDOWN_REQUALIFIED",
        "event_id": event_id,
        "setup_id": None,
        "position_id": None,
        "direction": None,
        "entry_price_estimate": None,
        "stop_price_estimate": None,
        "risk_points": None,
        "latest_closed_bar_time": at.isoformat(),
        "reason": "冷卻期滿後，程式重新完成大級錨、大小道氏、象限與合格型態認證。",
    }
    applied = apply_constitution_event(
        path,
        event,
        expected_latest_closed_bar_time=at.isoformat(),
    )
    state = applied.get("state")
    if not isinstance(state, dict):
        raise ProgramConstitutionError("交易憲法重認證沒有回傳有效狀態。")
    execution_event = {
        "event_type": "PROGRAM_CONSTITUTION_REQUALIFIED",
        "event_id": _stable_id("PROGRAM_CONSTITUTION_REQUALIFIED", event_id),
        "source_event_id": event_id,
        "requalified_at": at.isoformat(),
        "large_anchor_ref": evidence.get("large_anchor_ref"),
        "setup_key": evidence.get("candidate_setup_key"),
        "direction": evidence.get("candidate_direction"),
        "working_quadrant": evidence.get("working_quadrant"),
        "authority": PROGRAM_CONSTITUTION_AUTHORITY,
    }
    return state, execution_event


def retire_setups_for_constitution_lock(
    memory: Mapping[str, Any] | None,
    *,
    as_of: str,
) -> tuple[dict[str, Any], set[str]]:
    """Retire every pre-lock setup so expiry cannot resurrect it later."""

    result = dict(memory or {})
    retired: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw in result.get("active_setups", []):
        if not isinstance(raw, Mapping):
            continue
        setup = dict(raw)
        key = _optional_text(setup.get("setup_key"))
        if key:
            retired.add(key)
        if setup.get("stage") not in {"INVALIDATED", "NO_CHASE"}:
            setup["stage"] = "NO_CHASE"
        setup["reentry_status"] = "NOT_APPLICABLE"
        normalized.append(setup)
    result["active_setups"] = normalized
    result["as_of"] = as_of
    reentry = result.get("reentry")
    if isinstance(reentry, Mapping):
        result["reentry"] = {**dict(reentry), "status": "NOT_APPLICABLE"}
    notes = [str(item) for item in result.get("notes", []) if str(item)]
    marker = "交易憲法鎖定：舊setup全部退役，返場只接受冷卻後的新完整結構。"
    if marker not in notes:
        notes.append(marker)
    result["notes"] = notes
    return result, retired


def suppress_candidate_for_constitution_lock(
    ledger: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    audit: Mapping[str, Any],
) -> dict[str, Any]:
    result = dict(ledger)
    levels = dict(result.get("trade_levels") or {})
    candidate = levels.get("continuation_arm_candidate")
    if isinstance(candidate, Mapping):
        levels["constitution_filtered_candidate"] = dict(candidate)
    levels["continuation_arm_candidate"] = None
    levels["constitution_gate"] = {
        "status": "BLOCKED",
        "cooldown_until": snapshot.get("cooldown_until"),
        "cooldown_active": snapshot.get("cooldown_active"),
        "requalification_required": snapshot.get("requalification_required"),
        "reason_codes": list(audit.get("reason_codes") or []),
        "authority": PROGRAM_CONSTITUTION_AUTHORITY,
    }
    result["trade_levels"] = levels
    result["program_constitution"] = dict(snapshot)
    result["program_constitution_requalification_audit"] = dict(audit)
    return result


def _event_time(event: Mapping[str, Any]) -> datetime:
    raw = event.get("fill_time") or event.get("recorded_at") or event.get("signal_time")
    if not isinstance(raw, str) or not raw:
        raise ProgramConstitutionError("程式成交事件缺少可追溯時間。")
    return _aware(raw)


def _execution_realized_points(event: Mapping[str, Any]) -> float | None:
    entry = event.get("entry_price")
    fill = event.get("fill_price")
    if (
        isinstance(entry, bool)
        or not isinstance(entry, (int, float))
        or isinstance(fill, bool)
        or not isinstance(fill, (int, float))
    ):
        return None
    direction = str(event.get("direction") or "")
    if direction == "LONG":
        return float(fill) - float(entry)
    if direction == "SHORT":
        return float(entry) - float(fill)
    return None


def _stable_id(*parts: Any) -> str:
    material = "|".join(str(item) for item in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _aware(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ProgramConstitutionError("交易憲法時間格式無效。") from exc
    if parsed.tzinfo is None:
        raise ProgramConstitutionError("交易憲法時間必須包含時區。")
    return parsed


def _optional_aware(value: Any) -> datetime | None:
    return _aware(value) if isinstance(value, str) and value else None


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProgramConstitutionError(f"{field}不得為空。")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0:
        raise ProgramConstitutionError(f"{field}必須是正數。")
    return float(value)
