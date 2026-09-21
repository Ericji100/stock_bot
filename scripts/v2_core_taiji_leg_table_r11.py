"""R11 candidate: causal evidence contract for AI-proposed same-scale Taiji legs.

The program validates chronology, scale references and evidence availability.
The AI remains responsible for deciding whether a proposed segment really is
same-scale; passing this contract is not course correctness or trade approval.
"""

from __future__ import annotations

from datetime import date
from typing import Any


VERSION = "v2-core-taiji-leg-table-r11-candidate-r1"
_FIELDS = {
    "index", "role", "scale", "parent_anchor_id", "start_date", "end_date",
    "confirmed_on", "evidence_refs", "same_scale_reason",
}


def _date(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be an ISO date")
    return value


def validate_taiji_leg_table_asof(
    *, as_of: str, parent_anchor_id: str, controlling_scale: str,
    counted_legs: list[dict[str, Any]], available_evidence_refs: set[str],
) -> dict[str, Any]:
    """Check the proposed 1→2→3... table; never infer legs from pivots.

    Only legs explicitly marked by AI as sharing `controlling_scale` may enter
    `counted_legs`. Smaller internal pivots belong in separate episode evidence.
    A forming terminal leg has no confirmed end and may not be followed by a
    later counted leg.
    """
    as_of = _date(as_of, "as_of")
    if not isinstance(parent_anchor_id, str) or not parent_anchor_id:
        raise ValueError("parent_anchor_id required")
    if controlling_scale not in {"LARGE", "SMALL", "UNRESOLVED"}:
        raise ValueError("invalid controlling_scale")
    if not isinstance(available_evidence_refs, set) or not all(isinstance(ref, str) for ref in available_evidence_refs):
        raise ValueError("available_evidence_refs must be a string set")
    if controlling_scale == "UNRESOLVED":
        if counted_legs:
            raise ValueError("unresolved scale cannot claim counted same-scale legs")
        return {
            "gate_version": VERSION, "status": "SCALE_UNRESOLVED",
            "counted_leg_count": 0, "last_confirmed_leg": None,
            "trade_permission_granted": False,
        }
    if not isinstance(counted_legs, list) or not counted_legs:
        raise ValueError("known scale requires at least one proposed leg")
    previous_end: str | None = None
    for offset, leg in enumerate(counted_legs):
        if not isinstance(leg, dict) or set(leg) != _FIELDS:
            raise ValueError("each leg must use the exact evidence contract")
        index = offset + 1
        if type(leg["index"]) is not int or leg["index"] != index:
            raise ValueError("same-scale leg indexes must be contiguous from 1")
        if leg["role"] != ("ADVANCE" if index % 2 else "CORRECTION"):
            raise ValueError("leg direction does not follow advance/correction sequence")
        if leg["scale"] != controlling_scale or leg["parent_anchor_id"] != parent_anchor_id:
            raise ValueError("counted leg changed scale or parent anchor")
        start = _date(leg["start_date"], "leg.start_date")
        if start > as_of or (previous_end is not None and start < previous_end):
            raise ValueError("same-scale legs overlap backwards or start after AS-OF")
        if previous_end is None and offset > 0:
            raise ValueError("forming leg cannot have a later counted leg")
        end = leg["end_date"]
        confirmed = leg["confirmed_on"]
        if end is None:
            if confirmed is not None or index != len(counted_legs):
                raise ValueError("only terminal forming leg may omit end confirmation")
        else:
            end = _date(end, "leg.end_date")
            confirmed = _date(confirmed, "leg.confirmed_on")
            if not start <= end <= confirmed <= as_of:
                raise ValueError("leg endpoint was not causally confirmed by AS-OF")
        refs = leg["evidence_refs"]
        if not isinstance(refs, list) or not refs or len(refs) != len(set(refs)):
            raise ValueError("each leg needs distinct evidence references")
        if not set(refs) <= available_evidence_refs:
            raise ValueError("leg cites evidence unavailable in AS-OF packet")
        if not isinstance(leg["same_scale_reason"], str) or len(leg["same_scale_reason"].strip()) < 12:
            raise ValueError("AI must explain the same-scale relationship")
        previous_end = end
    return {
        "gate_version": VERSION,
        "status": "EVIDENCE_TABLE_VALID_NOT_SEMANTICALLY_PROVEN",
        "counted_leg_count": len(counted_legs),
        "last_confirmed_leg": next(
            (leg["index"] for leg in reversed(counted_legs) if leg["confirmed_on"] is not None), None
        ),
        "terminal_leg_forming": counted_legs[-1]["end_date"] is None,
        "trade_permission_granted": False,
    }
