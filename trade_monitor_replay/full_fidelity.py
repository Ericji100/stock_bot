from __future__ import annotations

import copy
import re
from datetime import datetime
from typing import Any, Mapping

from trade_monitor.analysis_contract import validate_analysis_payload
from trade_monitor.market_structure_state import (
    STATE_VERSION,
    market_structure_notification_reasons,
    validate_market_structure_transition,
)


class FullFidelityReplayError(ValueError):
    """A production-contract payload cannot safely enter replay state."""


VISIBLE_ANALYSIS_KEYS = {
    "notification_reason",
    "latest_closed_k_price_estimate",
    "latest_closed_k_details",
    "large_trend",
    "current_trend",
    "market_state",
    "pattern_observation",
    "missing_conditions_or_trigger",
    "entry_and_structural_stop",
    "risk_and_nearest_obstacle",
    "single_contract_management_or_prohibition",
}

_MACHINE_PIVOT = re.compile(
    r"kind=(?P<kind>HIGH|LOW|高|低)\s*[、,，]\s*"
    r"bar_time=(?P<time>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?[+-]\d{2}:\d{2})\s*[、,，]\s*"
    r"price=(?P<price>-?\d+(?:\.\d+)?)",
    flags=re.IGNORECASE,
)
_ISO_TIME = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})T(?P<hhmm>\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})"
)


def humanize_visible_analysis(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Remove internal field syntax from user-visible fields only.

    Machine-readable values inside constitution_event and market_structure_state
    remain byte-for-byte untouched so the production validators still receive
    their exact contract.
    """

    result = copy.deepcopy(dict(payload))
    for key in VISIBLE_ANALYSIS_KEYS:
        if key in result:
            result[key] = _visit_visible(result[key])
    return result


def validate_full_fidelity_payload(
    payload: Mapping[str, Any],
    *,
    previous_market_structure: Mapping[str, Any] | None,
    expected_as_of: str,
    expected_session_key: str,
    preopen: bool,
) -> tuple[dict[str, Any], list[str]]:
    """Apply the same production contract and state-transition checks in replay."""

    humanized = _normalize_replay_contract_bookkeeping(humanize_visible_analysis(payload))
    if "constitution_event" not in humanized:
        raise FullFidelityReplayError("正式v8輸出缺少constitution_event。")
    if "market_structure_state" not in humanized:
        raise FullFidelityReplayError("正式v8輸出缺少market_structure_state v6。")

    validated = validate_analysis_payload(humanized)
    structure = validated.get("market_structure_state")
    if not isinstance(structure, Mapping):
        raise FullFidelityReplayError("market_structure_state不得為空。")
    if structure.get("version") != STATE_VERSION:
        raise FullFidelityReplayError(f"market_structure_state必須使用正式state v{STATE_VERSION}。")
    if structure.get("session_key") != expected_session_key:
        raise FullFidelityReplayError("market_structure_state.session_key與回放時段不一致。")

    normalized = validate_market_structure_transition(
        previous_market_structure,
        structure,
        expected_as_of=expected_as_of,
    )
    reasons = market_structure_notification_reasons(previous_market_structure, normalized)
    validated["market_structure_state"] = normalized
    if reasons:
        validated["original_decision"] = "NOTIFY"
        reason_text = "、".join(reasons)
        if reason_text not in validated["notification_reason"]:
            validated["notification_reason"] = (
                f"{validated['notification_reason']}（系統強制通知：{reason_text}）"
            )

    if preopen and validated["constitution_event"]["event_type"] != "NONE":
        raise FullFidelityReplayError("盤前歷史快照不得追認已走完行情為模擬交易事件。")
    return validated, reasons


def _normalize_replay_contract_bookkeeping(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Clear non-semantic timestamps that contradict an explicitly inactive context.

    This does not infer a market state or alter active Taiji legs.  The formal
    validator requires an empty Taiji context to use a null change timestamp;
    models occasionally update only that timestamp while correctly clearing all
    lineage fields during a session reset.
    """

    result = copy.deepcopy(dict(payload))
    structure = result.get("market_structure_state")
    cclass = structure.get("cclass_context") if isinstance(structure, dict) else None
    taiji = cclass.get("taiji_context") if isinstance(cclass, dict) else None
    cleared_terminal_taiji = False
    if isinstance(taiji, dict) and taiji.get("active_leg_id") is None:
        retained_legs = taiji.get("legs")
        retained_legs = retained_legs if isinstance(retained_legs, list) else []
        only_terminal_history = all(
            isinstance(leg, Mapping) and leg.get("status") in {"COMPLETED", "INVALIDATED"}
            for leg in retained_legs
        )
        if cclass.get("engine_mode") != "TAIJI_ORDERED" and only_terminal_history:
            taiji["dynasty_id"] = None
            taiji["anchor_id"] = None
            taiji["legs"] = []
            taiji["assessment_changed_at"] = None
            cleared_terminal_taiji = bool(retained_legs)
    if cleared_terminal_taiji and isinstance(structure, dict):
        prospective = structure.get("prospective_context")
        evolution = prospective.get("taiji_evolution") if isinstance(prospective, dict) else None
        if isinstance(evolution, dict) and evolution.get("active_sequence") == "NONE":
            evolution["copy_outcomes"] = []
            evolution["correction_outcomes"] = []
            evolution["copy_amplitude_trend"] = "UNDEFINED"
            evolution["copy_duration_trend"] = "UNDEFINED"
            evolution["correction_amplitude_trend"] = "UNDEFINED"
            evolution["correction_duration_trend"] = "UNDEFINED"
    momentum = cclass.get("momentum_context") if isinstance(cclass, dict) else None
    if (
        isinstance(momentum, dict)
        and momentum.get("stage") == "NONE"
        and momentum.get("episode_id") is None
        and momentum.get("direction") is None
        and momentum.get("first_seen_at") is None
    ):
        momentum["stage_changed_at"] = None
    if (
        isinstance(momentum, dict)
        and not str(momentum.get("stage") or "").startswith("LIFE_DEATH_GATE")
    ):
        momentum["gate_balance"] = "NOT_APPLICABLE"
    scenario = structure.get("scenario_context") if isinstance(structure, dict) else None
    setup = scenario.get("setup") if isinstance(scenario, dict) else None
    if (
        isinstance(setup, dict)
        and setup.get("stage") == "NONE"
        and setup.get("pattern") == "NONE"
        and setup.get("setup_id") is None
        and setup.get("direction") is None
        and setup.get("first_seen_at") is None
    ):
        setup["stage_changed_at"] = None
    return result


def _visit_visible(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _visit_visible(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_visit_visible(item) for item in value]
    if isinstance(value, str):
        return _humanize_text(value)
    return value


def _humanize_text(text: str) -> str:
    def pivot(match: re.Match[str]) -> str:
        kind = match.group("kind").upper()
        label = "高點" if kind in {"HIGH", "高"} else "低點"
        hhmm = datetime.fromisoformat(match.group("time")).strftime("%H:%M")
        number = float(match.group("price"))
        price = f"{number:,.0f}" if number.is_integer() else f"{number:,.4f}".rstrip("0").rstrip(".")
        return f"{hhmm} {label} {price}點"

    result = _MACHINE_PIVOT.sub(pivot, text)
    result = _ISO_TIME.sub(lambda match: match.group("hhmm"), result)
    result = re.sub(r"\bbar_time=", "時間", result)
    result = re.sub(r"\bconfirmation_time=", "確認時間", result)
    result = re.sub(r"\bprice=(-?\d+(?:\.\d+)?)", r"價位\1", result)
    result = re.sub(r"\bkind=(?:HIGH|高)\b", "高點", result, flags=re.IGNORECASE)
    result = re.sub(r"\bkind=(?:LOW|低)\b", "低點", result, flags=re.IGNORECASE)
    return result
