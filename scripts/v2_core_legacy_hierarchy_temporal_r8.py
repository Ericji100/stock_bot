"""AS-OF temporal fact checks for distinct work-anchor and defense objects.

This gate checks observable chronology only.  Cross-scale overlap is legal;
it never decides causal control, scenario, trigger or trade permission.
"""

from __future__ import annotations

from datetime import date
from typing import Any


def check_temporal_facts(anchor: dict[str, Any], defense: dict[str, Any], as_of_text: str) -> dict[str, Any]:
    as_of = date.fromisoformat(as_of_text)
    anchor_start = date.fromisoformat(anchor["start_date"])
    defense_start = date.fromisoformat(defense["source_date"])
    defense_confirmed = date.fromisoformat(defense["confirmation_date"])
    errors = []
    if anchor_start > as_of:
        errors.append("anchor starts after AS-OF")
    if not (defense_start <= defense_confirmed <= as_of):
        errors.append("defense not confirmed by AS-OF close")
    if defense.get("scale") != "SMALL" or defense.get("side") != "LOW":
        errors.append("defense is not a small-scale low")
    anchor_status = anchor["status"]
    end_text = anchor.get("confirmed_end_date")
    if anchor_status == "CONFIRMED":
        if end_text is None:
            errors.append("confirmed anchor lacks end")
        else:
            end = date.fromisoformat(end_text)
            if not (anchor_start <= end <= as_of):
                errors.append("anchor end outside AS-OF chronology")
    elif anchor_status == "FORMING":
        if end_text is not None:
            errors.append("forming anchor has future/confirmed end")
        if anchor.get("observed_through") != as_of_text:
            errors.append("forming anchor not observed through AS-OF")
    else:
        errors.append("unknown anchor lifecycle")
    if errors:
        return {"status": "INVALID_AS_OF", "errors": errors, "causal_control_decided": False, "trade_permission_granted": False}
    if end_text and date.fromisoformat(end_text) < defense_start:
        relation = "ANCHOR_COMPLETED_BEFORE_DEFENSE"
    elif anchor_start <= defense_start:
        relation = "ANCHOR_SPANS_DEFENSE_START"
    else:
        relation = "ANCHOR_STARTED_AFTER_DEFENSE"
    return {
        "status": "LEGAL_AS_OF",
        "temporal_relation": relation,
        "cross_scale_overlap_possible": relation == "ANCHOR_SPANS_DEFENSE_START" and anchor["scale"] != defense["scale"],
        "causal_control_decided": False,
        "trade_permission_granted": False,
    }
