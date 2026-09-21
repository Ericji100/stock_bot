from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from trade_monitor.analysis_contract import validate_analysis_payload


class ReplayContractError(ValueError):
    pass


MEMORY_KEYS = {
    "version", "as_of", "session_key", "large_structure", "small_structure",
    "market_regime", "working_quadrant", "primary_scenario", "alternative_scenario",
    "active_setup", "position", "thesis", "recent_pivots", "notes",
}


def empty_replay_memory(*, as_of: str, session_key: str) -> dict[str, Any]:
    unavailable = {
        "direction": "UNDEFINED",
        "anchor": "尚未建立定錨。",
        "defense": "尚未建立道氏防線。",
        "invalidation": "等待可驗證結構。",
    }
    return {
        "version": 1,
        "as_of": as_of,
        "session_key": session_key,
        "large_structure": dict(unavailable),
        "small_structure": dict(unavailable),
        "market_regime": "尚未完成第一次歷史回放判讀。",
        "working_quadrant": "UNDEFINED",
        "primary_scenario": "等待AI依已揭露K棒建立主要情境。",
        "alternative_scenario": "等待AI依已揭露K棒建立備用情境。",
        "active_setup": None,
        "position": {
            "status": "FLAT",
            "entry_time": None,
            "entry_price": None,
            "stop_price": None,
            "reason": "尚未建立模擬持倉。",
        },
        "thesis": {
            "bias": "UNDEFINED",
            "maintain": "等待第一次判讀。",
            "downgrade": "等待第一次判讀。",
            "flip": "等待第一次判讀。",
        },
        "recent_pivots": [],
        "notes": ["這是精簡回放記憶，不是正式v8市場結構狀態。"],
    }


def validate_replay_envelope(
    value: Any,
    *,
    expected_as_of: str,
    expected_session_key: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"analysis", "memory"}:
        raise ReplayContractError("回放輸出頂層必須只有analysis與memory。")
    analysis = validate_analysis_payload(value["analysis"])
    analysis.pop("constitution_event", None)
    analysis.pop("market_structure_state", None)
    memory = _memory(value["memory"], expected_as_of=expected_as_of, expected_session_key=expected_session_key)
    return {"analysis": analysis, "memory": memory}


def _memory(value: Any, *, expected_as_of: str, expected_session_key: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != MEMORY_KEYS:
        raise ReplayContractError("memory欄位不完整或有額外欄位。")
    if value.get("version") != 1:
        raise ReplayContractError("memory.version必須為1。")
    if value.get("as_of") != expected_as_of:
        raise ReplayContractError("memory.as_of必須等於本輪最新已收盤K。")
    try:
        parsed = datetime.fromisoformat(str(value.get("as_of")))
    except ValueError as exc:
        raise ReplayContractError("memory.as_of不是有效時間。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReplayContractError("memory.as_of必須包含時區。")
    if value.get("session_key") != expected_session_key:
        raise ReplayContractError("memory.session_key與模擬時段不一致。")
    result = dict(value)
    result["large_structure"] = _structure(value["large_structure"], "large_structure")
    result["small_structure"] = _structure(value["small_structure"], "small_structure")
    result["market_regime"] = _text(value["market_regime"], "market_regime", 500)
    if value["working_quadrant"] not in {"Q1", "Q2", "Q3", "Q4", "TRANSITION", "UNDEFINED"}:
        raise ReplayContractError("working_quadrant無效。")
    result["primary_scenario"] = _text(value["primary_scenario"], "primary_scenario", 800)
    result["alternative_scenario"] = _text(value["alternative_scenario"], "alternative_scenario", 800)
    result["active_setup"] = _setup(value["active_setup"])
    result["position"] = _position(value["position"])
    result["thesis"] = _thesis(value["thesis"])
    result["recent_pivots"] = _text_list(value["recent_pivots"], "recent_pivots", 12, allow_empty=True)
    result["notes"] = _text_list(value["notes"], "notes", 8)
    return result


def _structure(value: Any, field: str) -> dict[str, Any]:
    keys = {"direction", "anchor", "defense", "invalidation"}
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ReplayContractError(f"{field}格式無效。")
    if value["direction"] not in {"BULL", "BEAR", "RANGE", "UNDEFINED"}:
        raise ReplayContractError(f"{field}.direction無效。")
    return {
        "direction": value["direction"],
        "anchor": _text(value["anchor"], f"{field}.anchor", 500),
        "defense": _text(value["defense"], f"{field}.defense", 500),
        "invalidation": _text(value["invalidation"], f"{field}.invalidation", 500),
    }


def _setup(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    keys = {"name", "status", "direction", "trigger", "stop", "expected_behavior", "invalidation"}
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ReplayContractError("active_setup格式無效。")
    if value["direction"] not in {"LONG", "SHORT", "NONE"}:
        raise ReplayContractError("active_setup.direction無效。")
    return {key: value[key] if key == "direction" else _text(value[key], f"active_setup.{key}", 500) for key in keys}


def _position(value: Any) -> dict[str, Any]:
    keys = {"status", "entry_time", "entry_price", "stop_price", "reason"}
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ReplayContractError("position格式無效。")
    status = value["status"]
    if status not in {"FLAT", "LONG", "SHORT"}:
        raise ReplayContractError("position.status無效。")
    if status == "FLAT" and any(value[key] is not None for key in ("entry_time", "entry_price", "stop_price")):
        raise ReplayContractError("空手時不得保留模擬進場與停損值。")
    if status != "FLAT" and any(value[key] is None for key in ("entry_time", "entry_price", "stop_price")):
        raise ReplayContractError("持倉時必須保存進場時間、進場價與停損價。")
    if value["entry_time"] is not None:
        if not isinstance(value["entry_time"], str):
            raise ReplayContractError("position.entry_time必須是含時區時間字串。")
        try:
            parsed_entry = datetime.fromisoformat(value["entry_time"])
        except ValueError as exc:
            raise ReplayContractError("position.entry_time不是有效時間。") from exc
        if parsed_entry.tzinfo is None or parsed_entry.utcoffset() is None:
            raise ReplayContractError("position.entry_time必須包含時區。")
    for key in ("entry_price", "stop_price"):
        if value[key] is not None and (isinstance(value[key], bool) or not isinstance(value[key], (int, float)) or value[key] <= 0):
            raise ReplayContractError(f"position.{key}必須是正數或null。")
    return dict(value)


def _thesis(value: Any) -> dict[str, Any]:
    keys = {"bias", "maintain", "downgrade", "flip"}
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ReplayContractError("thesis格式無效。")
    if value["bias"] not in {"BULL", "BEAR", "NEUTRAL", "CONDITIONAL", "UNDEFINED"}:
        raise ReplayContractError("thesis.bias無效。")
    return {key: value[key] if key == "bias" else _text(value[key], f"thesis.{key}", 500) for key in keys}


def _text_list(value: Any, field: str, limit: int, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty) or len(value) > limit:
        raise ReplayContractError(f"{field}格式無效。")
    return [_text(item, field, 500) for item in value]


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ReplayContractError(f"{field}文字無效。")
    return value.strip()
