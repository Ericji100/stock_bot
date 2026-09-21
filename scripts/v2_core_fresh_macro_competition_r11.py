"""R11 candidate: deterministic conflict gate for two AI structure hypotheses.

AI supplies independent FRESH/MACRO evidence. The program checks that the
mutually exclusive interpretations do not silently turn into an else-route.
No output from this module is itself a V2 trade permission.
"""

from __future__ import annotations

from datetime import date
from typing import Any


VERSION = "v2-core-fresh-macro-competition-r11-candidate-r1"
_FIELDS = {
    "as_of", "scenario", "verdict", "replicable_parent", "direction",
    "episode_stop_id", "position_role", "support_refs", "counter_refs", "reason",
}
_SCENARIOS = ("FRESH_Q1_EXPANSION", "MACRO_COPY_RESONANCE")


def _check_hypothesis(
    hypothesis: dict[str, Any], *, scenario: str, as_of: str,
    role: str, available_refs: set[str],
) -> None:
    if not isinstance(hypothesis, dict) or set(hypothesis) != _FIELDS:
        raise ValueError("hypothesis does not match R11 candidate contract")
    if hypothesis["scenario"] != scenario or hypothesis["as_of"] != as_of:
        raise ValueError("hypothesis scenario or AS-OF mismatch")
    if hypothesis["verdict"] not in {"PASS", "FAIL", "UNKNOWN"}:
        raise ValueError("invalid hypothesis verdict")
    if hypothesis["replicable_parent"] not in {"PASS", "FAIL", "UNKNOWN"}:
        raise ValueError("invalid replicable-parent verdict")
    if hypothesis["direction"] not in {"UP", "DOWN", "UNRESOLVED"}:
        raise ValueError("invalid direction")
    if hypothesis["position_role"] != role:
        raise ValueError("hypothesis tried to change frozen execution role")
    if hypothesis["episode_stop_id"] is not None and not isinstance(hypothesis["episode_stop_id"], str):
        raise ValueError("stop id must be string or null")
    for field in ("support_refs", "counter_refs"):
        refs = hypothesis[field]
        if not isinstance(refs, list) or len(refs) != len(set(refs)) or not set(refs) <= available_refs:
            raise ValueError(f"{field} must cite distinct AS-OF evidence")
    if not isinstance(hypothesis["reason"], str) or len(hypothesis["reason"].strip()) < 12:
        raise ValueError("hypothesis must explain its causal claim")
    if hypothesis["verdict"] == "PASS":
        expected_parent = "FAIL" if scenario == _SCENARIOS[0] else "PASS"
        if hypothesis["replicable_parent"] != expected_parent:
            raise ValueError("PASS contradicts the V2 FRESH/MACRO parent boundary")
        if hypothesis["direction"] != "UP" or not hypothesis["episode_stop_id"] or not hypothesis["support_refs"]:
            raise ValueError("PASS needs up direction, fixed stop and support")


def compete_fresh_macro_asof(
    *, as_of: str, frozen_position_role: str,
    fresh: dict[str, Any], macro: dict[str, Any],
    available_evidence_refs: set[str],
) -> dict[str, Any]:
    """Return one provisional route, or conservative conflict/unknown state."""
    try:
        if date.fromisoformat(as_of).isoformat() != as_of:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("as_of must be an ISO date") from exc
    if frozen_position_role not in {"MOTHER_OR_REENTRY", "ADD_1", "ADD_2", "ADD_3"}:
        raise ValueError("unresolved or invalid execution role")
    if not isinstance(available_evidence_refs, set):
        raise ValueError("available_evidence_refs must be a set")
    _check_hypothesis(fresh, scenario=_SCENARIOS[0], as_of=as_of, role=frozen_position_role, available_refs=available_evidence_refs)
    _check_hypothesis(macro, scenario=_SCENARIOS[1], as_of=as_of, role=frozen_position_role, available_refs=available_evidence_refs)
    passing = [row for row in (fresh, macro) if row["verdict"] == "PASS"]
    unknown = [row for row in (fresh, macro) if row["verdict"] == "UNKNOWN"]
    if len(passing) == 2:
        status, selected = "ADJUDICATION_REQUIRED_MUTUALLY_EXCLUSIVE_PASS", None
    elif passing and unknown:
        status, selected = "ADJUDICATION_REQUIRED_ALTERNATIVE_UNKNOWN", None
    elif len(passing) == 1:
        status, selected = "SINGLE_ROUTE_CANDIDATE_NOT_TRADE", passing[0]
    elif unknown:
        status, selected = "WAIT_STRUCTURE_UNKNOWN", None
    else:
        status, selected = "NO_FRESH_OR_MACRO_ROUTE", None
    return {
        "gate_version": VERSION,
        "as_of": as_of,
        "status": status,
        "provisional_scenario": selected["scenario"] if selected else None,
        "frozen_position_role": frozen_position_role,
        "frozen_episode_stop_id": selected["episode_stop_id"] if selected else None,
        "trade_permission_granted": False,
        "hypothesis_verdicts": {
            fresh["scenario"]: fresh["verdict"], macro["scenario"]: macro["verdict"],
        },
    }
