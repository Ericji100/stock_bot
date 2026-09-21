from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .bridge import DeliveryFileLock


TAIPEI = ZoneInfo("Asia/Taipei")
STATE_VERSION = 1
DEFAULT_STATE_PATH = Path(".runtime/trade_monitor/constitution_state.json")
MAX_SETUP_ENTRIES = 2
COOLDOWN_MINUTES = 30
MAX_PROCESSED_EVENTS = 2000
EVENT_TYPES = {"NONE", "SIM_ENTER", "SIM_STOP", "SIM_EXIT", "COOLDOWN_REQUALIFIED"}
DIRECTIONS = {"LONG", "SHORT"}


class ConstitutionStateError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def trading_day_for(value: datetime) -> str:
    local = _as_taipei(value)
    label = local.date() + timedelta(days=1) if local.time() >= time(15, 0) else local.date()
    while label.weekday() >= 5:
        label += timedelta(days=1)
    return label.isoformat()


def entry_window_open(value: datetime) -> bool:
    local_time = _as_taipei(value).time()
    return local_time >= time(15, 0) or local_time < time(13, 45)


def load_constitution_snapshot(path: Path, *, at: datetime) -> dict[str, Any]:
    local = _as_taipei(at)
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        with DeliveryFileLock(lock_path):
            state, changed = _load_or_roll_state(path, local)
            if changed:
                _write_state_atomic(path, state)
            return _public_snapshot(state, local)
    except ConstitutionStateError as exc:
        return _locked_snapshot(local, exc.code)


def apply_constitution_event(
    path: Path,
    event: Mapping[str, Any],
    *,
    expected_latest_closed_bar_time: str,
) -> dict[str, Any]:
    normalized = validate_constitution_event(
        event,
        expected_latest_closed_bar_time=expected_latest_closed_bar_time,
    )
    event_time = _parse_time(normalized["latest_closed_bar_time"])
    lock_path = path.with_suffix(path.suffix + ".lock")
    with DeliveryFileLock(lock_path):
        state, changed = _load_or_roll_state(path, event_time)
        event_type = normalized["event_type"]
        if event_type == "NONE":
            if changed:
                _write_state_atomic(path, state)
            return {
                "ok": True,
                "status": "no_change",
                "event_key": None,
                "state": _public_snapshot(state, event_time),
            }

        event_key = _event_key(normalized)
        if event_key in state["processed_events"]:
            if changed:
                _write_state_atomic(path, state)
            return {
                "ok": True,
                "status": "duplicate",
                "event_key": event_key,
                "state": _public_snapshot(state, event_time),
            }

        _apply_transition(state, normalized, event_time)
        state["processed_events"][event_key] = {
            "event_type": event_type,
            "event_id": normalized["event_id"],
            "processed_at": event_time.isoformat(),
        }
        _prune_processed_events(state["processed_events"])
        state["updated_at"] = event_time.isoformat()
        _write_state_atomic(path, state)
        return {
            "ok": True,
            "status": "applied",
            "event_key": event_key,
            "state": _public_snapshot(state, event_time),
        }


def validate_constitution_event(
    event: Mapping[str, Any],
    *,
    expected_latest_closed_bar_time: str | None = None,
) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise ConstitutionStateError("constitution_event_invalid", "constitution_event must be an object")
    required = {
        "event_type",
        "event_id",
        "setup_id",
        "position_id",
        "direction",
        "entry_price_estimate",
        "stop_price_estimate",
        "risk_points",
        "latest_closed_bar_time",
        "reason",
    }
    if set(event) != required:
        raise ConstitutionStateError("constitution_event_invalid", "constitution_event has invalid fields")

    event_type = _required_text(event["event_type"], "event_type")
    if event_type not in EVENT_TYPES:
        raise ConstitutionStateError("constitution_event_invalid", "constitution event type is invalid")
    event_id = _required_text(event["event_id"], "event_id")
    reason = _required_text(event["reason"], "reason")
    latest = _required_text(event["latest_closed_bar_time"], "latest_closed_bar_time")
    parsed_latest = _parse_time(latest)
    if expected_latest_closed_bar_time:
        expected = _parse_time(expected_latest_closed_bar_time)
        if parsed_latest != expected:
            raise ConstitutionStateError(
                "constitution_event_time_mismatch",
                "constitution event is not anchored to the prepared latest closed bar",
            )

    setup_id = _optional_text(event["setup_id"], "setup_id")
    position_id = _optional_text(event["position_id"], "position_id")
    direction = _optional_text(event["direction"], "direction")
    if direction is not None and direction not in DIRECTIONS:
        raise ConstitutionStateError("constitution_event_invalid", "constitution direction is invalid")

    entry_price = _optional_positive_number(event["entry_price_estimate"], "entry_price_estimate")
    stop_price = _optional_positive_number(event["stop_price_estimate"], "stop_price_estimate")
    risk_points = _optional_positive_number(event["risk_points"], "risk_points")

    if event_type == "SIM_ENTER":
        if not setup_id or not position_id or direction not in DIRECTIONS:
            raise ConstitutionStateError(
                "constitution_event_invalid",
                "SIM_ENTER requires setup_id, position_id and direction",
            )
        if entry_price is None or stop_price is None or risk_points is None:
            raise ConstitutionStateError(
                "constitution_event_invalid",
                "SIM_ENTER requires entry, stop and risk estimates",
            )
    elif event_type in {"SIM_STOP", "SIM_EXIT"}:
        if not position_id:
            raise ConstitutionStateError(
                "constitution_event_invalid",
                f"{event_type} requires position_id",
            )

    return {
        "event_type": event_type,
        "event_id": event_id,
        "setup_id": setup_id,
        "position_id": position_id,
        "direction": direction,
        "entry_price_estimate": entry_price,
        "stop_price_estimate": stop_price,
        "risk_points": risk_points,
        "latest_closed_bar_time": parsed_latest.isoformat(),
        "reason": reason,
    }


def _apply_transition(state: dict[str, Any], event: Mapping[str, Any], at: datetime) -> None:
    event_type = event["event_type"]
    position = state.get("simulated_position")
    cooldown = _optional_state_time(state.get("cooldown_until"))

    if event_type == "SIM_ENTER":
        if not entry_window_open(at):
            raise ConstitutionStateError("entry_window_closed", "new simulated entries are disabled from 13:45 to 15:00")
        if position is not None:
            raise ConstitutionStateError("position_already_open", "a simulated position is already open")
        if cooldown is not None and at < cooldown:
            raise ConstitutionStateError("cooldown_active", "the 30-minute trading cooldown is active")
        if state.get("requalification_required") is True:
            raise ConstitutionStateError("requalification_required", "market structure must be requalified after cooldown")
        setup_id = str(event["setup_id"])
        setup_entries = state.setdefault("setup_entry_counts", {})
        if int(setup_entries.get(setup_id, 0)) >= MAX_SETUP_ENTRIES:
            raise ConstitutionStateError("setup_reentry_limit", "the original setup has used its one allowed re-entry")

        state["simulated_entry_count"] = int(state["simulated_entry_count"]) + 1
        setup_entries[setup_id] = int(setup_entries.get(setup_id, 0)) + 1
        state["simulated_position"] = {
            "position_id": event["position_id"],
            "setup_id": setup_id,
            "direction": event["direction"],
            "entry_price_estimate": event["entry_price_estimate"],
            "stop_price_estimate": event["stop_price_estimate"],
            "risk_points": event["risk_points"],
            "opened_at": at.isoformat(),
        }
        return

    if event_type in {"SIM_STOP", "SIM_EXIT"}:
        if not isinstance(position, dict) or position.get("position_id") != event["position_id"]:
            raise ConstitutionStateError("position_mismatch", "constitution event does not match the open simulated position")
        state["simulated_position"] = None
        if event_type == "SIM_STOP":
            state["consecutive_simulated_stops"] = int(state["consecutive_simulated_stops"]) + 1
            if int(state["consecutive_simulated_stops"]) >= 3:
                state["cooldown_until"] = (at + timedelta(minutes=COOLDOWN_MINUTES)).isoformat()
                state["requalification_required"] = True
        else:
            state["consecutive_simulated_stops"] = 0
        return

    if event_type == "COOLDOWN_REQUALIFIED":
        if position is not None:
            raise ConstitutionStateError("position_already_open", "cannot requalify while a simulated position is open")
        if cooldown is None or state.get("requalification_required") is not True:
            raise ConstitutionStateError("requalification_not_required", "no cooldown requalification is pending")
        if at < cooldown:
            raise ConstitutionStateError("cooldown_active", "the 30-minute trading cooldown is active")
        state["requalification_required"] = False
        state["consecutive_simulated_stops"] = 0
        return

    raise ConstitutionStateError("constitution_event_invalid", "unsupported constitution event")


def _load_or_roll_state(path: Path, at: datetime) -> tuple[dict[str, Any], bool]:
    desired_day = trading_day_for(at)
    if not path.exists():
        return _new_state(desired_day, at), True
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConstitutionStateError("constitution_state_invalid", "constitution state is invalid; trading is locked") from exc
    _validate_state(payload)
    if payload["trading_day"] != desired_day:
        return _new_state(desired_day, at), True
    return payload, False


def _new_state(trading_day: str, at: datetime) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "trading_day": trading_day,
        "simulated_entry_count": 0,
        "consecutive_simulated_stops": 0,
        "cooldown_until": None,
        "requalification_required": False,
        "simulated_position": None,
        "setup_entry_counts": {},
        "processed_events": {},
        "updated_at": _as_taipei(at).isoformat(),
    }


def _validate_state(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ConstitutionStateError("constitution_state_invalid", "constitution state is invalid; trading is locked")
    required = {
        "version",
        "trading_day",
        "simulated_entry_count",
        "consecutive_simulated_stops",
        "cooldown_until",
        "requalification_required",
        "simulated_position",
        "setup_entry_counts",
        "processed_events",
        "updated_at",
    }
    if set(payload) != required or payload.get("version") != STATE_VERSION:
        raise ConstitutionStateError("constitution_state_invalid", "constitution state is invalid; trading is locked")
    try:
        date.fromisoformat(str(payload["trading_day"]))
        entries = int(payload["simulated_entry_count"])
        stops = int(payload["consecutive_simulated_stops"])
    except (TypeError, ValueError) as exc:
        raise ConstitutionStateError("constitution_state_invalid", "constitution state is invalid; trading is locked") from exc
    if entries < 0 or stops < 0:
        raise ConstitutionStateError("constitution_state_invalid", "constitution state is invalid; trading is locked")
    if not isinstance(payload["requalification_required"], bool):
        raise ConstitutionStateError("constitution_state_invalid", "constitution state is invalid; trading is locked")
    if not isinstance(payload["setup_entry_counts"], dict) or not isinstance(payload["processed_events"], dict):
        raise ConstitutionStateError("constitution_state_invalid", "constitution state is invalid; trading is locked")
    if payload["simulated_position"] is not None and not isinstance(payload["simulated_position"], dict):
        raise ConstitutionStateError("constitution_state_invalid", "constitution state is invalid; trading is locked")
    _optional_state_time(payload["cooldown_until"])
    _parse_time(payload["updated_at"])


def _public_snapshot(state: Mapping[str, Any], at: datetime) -> dict[str, Any]:
    cooldown = _optional_state_time(state.get("cooldown_until"))
    local = _as_taipei(at)
    requalification = state.get("requalification_required") is True
    cooldown_active = cooldown is not None and local < cooldown
    locked = cooldown_active or requalification or not entry_window_open(local)
    return {
        "ok": True,
        "version": STATE_VERSION,
        "trading_day": state["trading_day"],
        "simulated_entry_count": int(state["simulated_entry_count"]),
        "remaining_entry_quota": None,
        "consecutive_simulated_stops": int(state["consecutive_simulated_stops"]),
        "cooldown_until": None if cooldown is None else cooldown.isoformat(),
        "cooldown_active": cooldown_active,
        "requalification_required": requalification,
        "entry_window_open": entry_window_open(local),
        "trading_locked": locked,
        "simulated_position": state.get("simulated_position"),
        "setup_entry_counts": dict(state.get("setup_entry_counts") or {}),
    }


def _locked_snapshot(at: datetime, reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "version": STATE_VERSION,
        "trading_day": trading_day_for(at),
        "simulated_entry_count": None,
        "remaining_entry_quota": 0,
        "consecutive_simulated_stops": None,
        "cooldown_until": None,
        "cooldown_active": False,
        "requalification_required": True,
        "entry_window_open": entry_window_open(at),
        "trading_locked": True,
        "simulated_position": None,
        "setup_entry_counts": {},
        "reason": reason,
    }


def _event_key(event: Mapping[str, Any]) -> str:
    identity = {
        "event_type": event["event_type"],
        "event_id": event["event_id"],
        "setup_id": event["setup_id"],
        "position_id": event["position_id"],
        "latest_closed_bar_time": event["latest_closed_bar_time"],
    }
    material = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _prune_processed_events(events: dict[str, Any]) -> None:
    if len(events) <= MAX_PROCESSED_EVENTS:
        return
    ordered = sorted(events.items(), key=lambda item: str(item[1].get("processed_at") or ""), reverse=True)
    events.clear()
    events.update(ordered[:MAX_PROCESSED_EVENTS])


def _write_state_atomic(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise ConstitutionStateError("constitution_state_write_failed", "constitution state could not be saved") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConstitutionStateError("constitution_event_invalid", f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConstitutionStateError("constitution_event_invalid", f"{field} must be null or a non-empty string")
    return value.strip()


def _optional_positive_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0:
        raise ConstitutionStateError("constitution_event_invalid", f"{field} must be null or a positive number")
    return float(value)


def _parse_time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConstitutionStateError("constitution_time_invalid", "constitution timestamps must be valid ISO timestamps") from exc
    if parsed.tzinfo is None:
        raise ConstitutionStateError("constitution_time_invalid", "constitution timestamps must include a timezone")
    return parsed.astimezone(TAIPEI)


def _optional_state_time(value: Any) -> datetime | None:
    if value is None:
        return None
    return _parse_time(value)


def _as_taipei(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ConstitutionStateError("constitution_time_invalid", "constitution timestamps must include a timezone")
    return value.astimezone(TAIPEI)
