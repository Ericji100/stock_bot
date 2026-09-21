"""Candidate5 packets: exact scale-aware campaign and stop linkage.

V4 and V5 remain byte-for-byte frozen.  This wrapper activates V5's scoped
comparison-window correction, then changes only the objective graph binding:

* anchors inherit campaigns only from the same scale and direction;
* every episode stop is the defense established by the relation's current
  attack, never an unrelated latest active stop;
* Mature/Macro campaign stops belong to the exact parent campaign and scale;
* missing links remain explicit ``None`` values and fail route sufficiency.

The AI prompt, atomic schema, scenario precedence, trading gates, execution,
position sizing, and exit rules are unchanged.
"""
from __future__ import annotations

from collections import Counter
import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from . import hybrid_v3_atomic_packets_v4 as _v4
    from . import hybrid_v3_atomic_packets_v5 as _v5
except ImportError:  # direct script import
    from scripts import hybrid_v3_atomic_packets_v4 as _v4
    from scripts import hybrid_v3_atomic_packets_v5 as _v5


BUILDER_VERSION = "hybrid-v3-atomic-packets-v6"
BUILDER_STATUS = "CANDIDATE_FOR_OUTCOME_BLIND_CAUSAL_VALIDATION"
BASE_BUILDER_VERSION = "hybrid-v3-atomic-packets-v5"
BASE_BUILDER_SHA256 = "aee29455083defe3b2846f43c8836d3a5952127ef94a5e9af55067e293eef72e"
FIX_CODES = (
    "SCALE_AWARE_ANCHOR_CAMPAIGN_ASSIGNMENT",
    "CURRENT_ATTACK_EXACT_EPISODE_STOP_LINK",
    "PARENT_CAMPAIGN_EXACT_STOP_LINK",
    "EXACT_STOP_LINK_ROUTE_SUFFICIENCY",
)
_WRAPPER_FILE = Path(__file__).resolve()
_BASE_FILE = Path(_v5.__file__).resolve()
_ORIGINAL_OBJECTIVE_FACTS = _v4._objective_facts
_ORIGINAL_ASSERT_ANONYMOUS_AND_CAUSAL = _v4.assert_anonymous_and_causal


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def assert_base_builder_frozen() -> None:
    if _file_sha256(_BASE_FILE) != BASE_BUILDER_SHA256:
        raise RuntimeError("V5 base builder bytes changed")
    if _v4.BUILDER_VERSION == BUILDER_VERSION:
        if _file_sha256(_v5._BASE_FILE) != _v5.BASE_BUILDER_SHA256:
            raise RuntimeError("V4 dependency bytes changed")
    else:
        _v5.assert_base_builder_frozen()


def _unresolved_campaign(scale: str, direction: str) -> str:
    return f"UNRESOLVED_CAMPAIGN:{scale}:{direction}"


def _anchor_candidates(
    objective: dict[str, Any],
    all_pivots: Sequence[dict[str, Any]],
    completed_cycles: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build anchors with campaign ownership partitioned by objective scale."""

    pivot_by_ref = {str(row["ref"]): row for row in all_pivots}
    boundaries: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for defense in objective.get("defenses") or []:
        direction = "UP" if defense["side"] == "BULLISH" else "DOWN"
        scale = str(defense["scale"])
        origin = pivot_by_ref.get(str(defense["source_pivot_ref"]))
        if origin is None or str(origin.get("scale")) != scale:
            continue
        item = (str(origin["source_date"]), str(defense["campaign_id"]))
        rows = boundaries.setdefault((scale, direction), [])
        if item not in rows:
            rows.append(item)
    for rows in boundaries.values():
        rows.sort()

    def campaign_for(direction: str, scale: str, start_date: str) -> str:
        eligible = [
            campaign
            for day, campaign in boundaries.get((scale, direction), [])
            if day <= start_date
        ]
        return eligible[-1] if eligible else _unresolved_campaign(scale, direction)

    full: list[dict[str, Any]] = []
    for scale in ("SMALL", "LARGE"):
        stream = sorted(
            (row for row in all_pivots if row["scale"] == scale),
            key=lambda row: (
                str(row["source_date"]),
                str(row["confirmation_date"]),
                str(row["ref"]),
            ),
        )
        for left, right in zip(stream, stream[1:]):
            if left["side"] == right["side"]:
                continue
            direction = "UP" if left["side"] == "LOW" else "DOWN"
            raw = {
                "hypothesis_type": "PIVOT_LEG",
                "scale": scale,
                "direction": direction,
                "start_ref": left["ref"],
                "end_ref": right["ref"],
                "start_date": str(left["source_date"]),
                "end_date": str(right["source_date"]),
                "available_on": max(
                    str(left["confirmation_date"]), str(right["confirmation_date"])
                ),
                "amplitude": abs(float(right["price"]) - float(left["price"])),
                "macd_support_only": False,
                "campaign_id": campaign_for(direction, scale, str(left["source_date"])),
            }
            raw["objective_hypothesis_id"] = "H-" + _v4.canonical_sha256(raw)[:20]
            full.append(raw)
    for cycle in completed_cycles:
        if cycle.get("status") != "CONFIRMED" or not cycle.get("end"):
            continue
        direction = "UP" if str(cycle["sign"]).upper() == "POSITIVE" else "DOWN"
        start_ref = _v4._bar_ref(
            str(cycle["low_date"] if direction == "UP" else cycle["high_date"])
        )
        end_ref = _v4._bar_ref(
            str(cycle["high_date"] if direction == "UP" else cycle["low_date"])
        )
        raw = {
            "hypothesis_type": "MACD_SKELETON",
            "scale": "UNASSIGNED",
            "direction": direction,
            "start_ref": start_ref,
            "end_ref": end_ref,
            "start_date": str(cycle["start"]),
            "end_date": str(cycle["end"]),
            "available_on": str(cycle["end"]),
            "amplitude": abs(float(cycle["high"]) - float(cycle["low"])),
            "macd_support_only": True,
            "campaign_id": _unresolved_campaign("UNASSIGNED", direction),
        }
        raw["objective_hypothesis_id"] = "H-" + _v4.canonical_sha256(raw)[:20]
        full.append(raw)

    full.sort(
        key=lambda row: (str(row["available_on"]), str(row["objective_hypothesis_id"]))
    )
    counters: Counter[tuple[str, str, str]] = Counter()
    for row in full:
        key = (str(row["scale"]), str(row["campaign_id"]), str(row["direction"]))
        counters[key] += 1
        number = counters[key]
        row["same_direction_attack_number"] = number
        row["taiji_generation"] = {
            1: "ANCHOR_LEG_1",
            2: "COPY_LEG_3",
            3: "COPY_LEG_5",
        }.get(number, "LATER_GENERATION")
        row["completed_prior_copy_count"] = max(0, number - 2)

    source: list[dict[str, Any]] = []
    for hypothesis_type, scale, limit in (
        ("PIVOT_LEG", "SMALL", 4),
        ("PIVOT_LEG", "LARGE", 4),
        ("MACD_SKELETON", "UNASSIGNED", 2),
    ):
        group = [
            row
            for row in full
            if row.get("hypothesis_type") == hypothesis_type
            and row.get("scale") == scale
        ]
        source.extend(group[-limit:])
    source.sort(
        key=lambda row: (str(row["available_on"]), str(row["objective_hypothesis_id"]))
    )
    rows: list[dict[str, Any]] = []
    for hypothesis in source:
        hypothesis_id = str(hypothesis["objective_hypothesis_id"])
        rows.append(
            {
                "candidate_id": "ANCHOR_CANDIDATE:A-"
                + _v4.canonical_sha256(hypothesis_id)[:16],
                "objective_hypothesis_id": hypothesis_id,
                "hypothesis_type": str(hypothesis["hypothesis_type"]),
                "scale": str(hypothesis["scale"]),
                "direction_hint": str(hypothesis["direction"]),
                "start_ref": str(hypothesis["start_ref"]),
                "end_ref": str(hypothesis["end_ref"]),
                "available_on": str(hypothesis["available_on"]),
                "amplitude": _v4._number(hypothesis.get("amplitude")),
                "macd_support_only": bool(hypothesis.get("macd_support_only", False)),
                "campaign_id": str(hypothesis["campaign_id"]),
                "taiji_generation": str(hypothesis["taiji_generation"]),
                "same_direction_attack_number": int(
                    hypothesis["same_direction_attack_number"]
                ),
                "completed_prior_copy_count": int(
                    hypothesis["completed_prior_copy_count"]
                ),
                "pivot_definition": _v4.PIVOT_SCALE_DISCLOSURE,
                "asserted_valid_anchor": False,
            }
        )
    return rows


def _exact_episode_stop(
    relation: Mapping[str, Any],
    *,
    objective: Mapping[str, Any],
    stop_candidates: Sequence[Mapping[str, Any]],
) -> str | None:
    attacks = {
        str(row.get("attack_id")): row
        for row in objective.get("attacks") or []
        if isinstance(row, dict) and row.get("attack_id")
    }
    attack = attacks.get(str(relation.get("current_attack_ref") or ""))
    if not attack:
        return None
    if (
        attack.get("direction") != "UP"
        or attack.get("confirmed_on") != objective.get("as_of")
        or attack.get("scale") != relation.get("current_scale")
        or attack.get("defense_result") != "ESTABLISHED"
        or not attack.get("defense_id")
    ):
        return None
    matches = [
        row
        for row in stop_candidates
        if row.get("defense_ref") == attack.get("defense_id")
        and row.get("side") == "BULLISH"
        and row.get("scale") == attack.get("scale")
        and row.get("control_pivot_ref") == attack.get("control_pivot_ref")
        and row.get("established_on") == attack.get("confirmed_on")
    ]
    return str(matches[0]["candidate_id"]) if len(matches) == 1 else None


def _exact_parent_campaign_stop(
    relation: Mapping[str, Any],
    *,
    anchors: Mapping[str, Mapping[str, Any]],
    stop_candidates: Sequence[Mapping[str, Any]],
) -> str | None:
    parent = anchors.get(str(relation.get("parent_anchor_ref") or ""))
    if not parent:
        return None
    campaign_id = relation.get("campaign_id")
    parent_scale = relation.get("parent_scale")
    if (
        campaign_id in {None, ""}
        or str(campaign_id).startswith("UNRESOLVED_CAMPAIGN:")
        or parent.get("campaign_id") != campaign_id
        or parent.get("scale") != parent_scale
    ):
        return None
    matches = [
        row
        for row in stop_candidates
        if row.get("side") == "BULLISH"
        and row.get("scale") == parent_scale
        and row.get("campaign_id") == campaign_id
    ]
    return str(matches[0]["candidate_id"]) if len(matches) == 1 else None


def _scenario_hypotheses(
    candidates: dict[str, Any],
    *,
    position_role: str,
    objective: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Keep every typed relation while binding stops to its exact graph."""

    anchors = {row["candidate_id"]: row for row in candidates["anchor_candidates"]}
    relations = candidates["relation_candidates"]
    stops = candidates["stop_candidates"]
    result = {scenario: [] for scenario in _v4.SCENARIOS}
    for scenario in _v4.SCENARIOS:
        for relation in relations:
            if scenario not in set(relation.get("eligible_scenarios") or []):
                continue
            if (
                relation.get("current_direction") != "UP"
                or relation.get("current_available_on") != objective["as_of"]
                or not relation.get("current_attack_ref")
            ):
                continue
            relation_type = str(relation.get("relation_type") or "")
            route_shape_valid = {
                "BEAR_REVERSAL_LEFT_RIGHT": bool(
                    relation_type == "BEAR_DOWN_PARENT_TO_CURRENT_SMALL_UP"
                    and relation.get("parent_direction") == "DOWN"
                    and relation.get("parent_scale") == "LARGE"
                    and relation.get("current_scale") == "SMALL"
                    and relation.get("left_right_phase") in {"LR", "RR"}
                    and relation.get("large_bear_defense_ref")
                ),
                "MACRO_COPY_RESONANCE": bool(
                    relation_type == "CURRENT_UP_COPY_TRIGGER"
                    and relation.get("parent_direction") == "UP"
                    and relation.get("correction_direction") == "DOWN"
                    and relation.get("parent_scale") == "LARGE"
                    and relation.get("correction_scale") == "LARGE"
                    and relation.get("current_scale") == "SMALL"
                    and relation.get("completed_prior_copy_count") == 0
                ),
                "MATURE_TREND_PULLBACK": bool(
                    relation_type == "CURRENT_UP_COPY_TRIGGER"
                    and relation.get("parent_direction") == "UP"
                    and relation.get("correction_direction") == "DOWN"
                    and relation.get("parent_scale") == "LARGE"
                    and relation.get("correction_scale") == "LARGE"
                    and relation.get("current_scale") == "SMALL"
                    and isinstance(relation.get("completed_prior_copy_count"), int)
                    and relation["completed_prior_copy_count"] >= 1
                ),
                "FRESH_Q1_EXPANSION": bool(
                    relation_type == "FRESH_UP_ANCHOR_CURRENT_TRIGGER"
                    and relation.get("parent_anchor_ref") is None
                    and relation.get("completed_prior_copy_count") == 0
                ),
            }[scenario]
            if not route_shape_valid:
                continue
            parent_ref = relation.get("parent_anchor_ref")
            current_ref = relation.get("current_leg_ref")
            anchor_ref = (
                current_ref
                if scenario == "FRESH_Q1_EXPANSION" and current_ref in anchors
                else (parent_ref if parent_ref in anchors else None)
            )
            episode_ref = _exact_episode_stop(
                relation, objective=objective, stop_candidates=stops
            )
            if scenario in {"MATURE_TREND_PULLBACK", "MACRO_COPY_RESONANCE"}:
                campaign_ref = _exact_parent_campaign_stop(
                    relation, anchors=anchors, stop_candidates=stops
                )
            elif scenario == "FRESH_Q1_EXPANSION":
                episode = next(
                    (row for row in stops if row.get("candidate_id") == episode_ref),
                    None,
                )
                anchor = anchors.get(str(anchor_ref or ""))
                campaign_ref = (
                    episode_ref
                    if episode
                    and anchor
                    and episode.get("campaign_id") == anchor.get("campaign_id")
                    and episode.get("scale") == anchor.get("scale")
                    else None
                )
            else:
                campaign_ref = episode_ref
            values = {
                "scenario": scenario,
                "anchor_ref": anchor_ref,
                "relation_ref": relation["candidate_id"],
                "episode_stop_ref": episode_ref,
                "campaign_stop_ref": campaign_ref,
                "position_role": position_role,
                "taiji_generation": relation["taiji_generation"],
                "same_direction_attack_number": relation[
                    "same_direction_attack_number"
                ],
                "completed_prior_copy_count": relation[
                    "completed_prior_copy_count"
                ],
            }
            result[scenario].append(
                {
                    "hypothesis_id": "SH-" + _v4.canonical_sha256(values)[:20],
                    **{key: value for key, value in values.items() if key != "scenario"},
                }
            )
    return result


def _complete_exact_hypothesis(hypothesis: Mapping[str, Any]) -> bool:
    return bool(
        hypothesis.get("relation_ref")
        and hypothesis.get("episode_stop_ref")
        and hypothesis.get("campaign_stop_ref")
    )


def _objective_facts(**kwargs: Any) -> dict[str, Any]:
    """Recompute only route sufficiency and stop-derived program flags."""

    facts = _ORIGINAL_OBJECTIVE_FACTS(**kwargs)
    hypotheses = facts["scenario_hypotheses"]
    sufficiency = facts["data_sufficiency_by_route"]
    complete_by_scenario = {
        scenario: [row for row in hypotheses[scenario] if _complete_exact_hypothesis(row)]
        for scenario in _v4.SCENARIOS
    }
    for scenario in _v4.SCENARIOS:
        row = sufficiency[scenario]
        if row["status"] is True and not complete_by_scenario[scenario]:
            row["status"] = False
            row["reason_code"] = "INSUFFICIENT_ROUTE_OBJECTIVE_EVIDENCE"

    all_hypotheses = [row for rows in hypotheses.values() for row in rows]
    exact_episode_refs = {
        str(row["episode_stop_ref"])
        for row in all_hypotheses
        if row.get("episode_stop_ref")
    }
    old_risk_executable = bool(facts.get("risk_executable"))
    facts["stop_causal_fields_valid"] = bool(exact_episode_refs)
    facts["risk_executable"] = bool(exact_episode_refs and old_risk_executable)
    facts["phase_stop_causal"] = any(
        row.get("episode_stop_ref")
        for row in hypotheses["BEAR_REVERSAL_LEFT_RIGHT"]
    )
    facts["fresh_anchor_stop_causal"] = any(
        row.get("episode_stop_ref") for row in hypotheses["FRESH_Q1_EXPANSION"]
    )

    material_signatures = {
        _v4.canonical_sha256(
            {
                "signal_event_ref": facts.get("signal_event_ref"),
                "anchor_ref": hypothesis.get("anchor_ref"),
                "relation_ref": hypothesis.get("relation_ref"),
                "episode_stop_ref": hypothesis.get("episode_stop_ref"),
                "campaign_stop_ref": hypothesis.get("campaign_stop_ref"),
                "position_role": hypothesis.get("position_role"),
            }
        )
        for scenario, rows in hypotheses.items()
        if sufficiency[scenario]["status"] is True
        for hypothesis in rows
    }
    material_conflict = len(material_signatures) > 1
    preserved = {
        reason
        for reason in facts.get("wait_boundary_reasons") or []
        if reason in {"INITIAL_SELECTION_BOUNDARY", "RESELECTION_BOUNDARY"}
    }
    if facts.get("trigger_completed") and not exact_episode_refs:
        preserved.add("UP_ATTACK_WITHOUT_CAUSAL_STOP")
    if facts.get("trigger_completed") and not any(
        row["status"] is True for row in sufficiency.values()
    ):
        preserved.add("UP_ATTACK_WITH_INSUFFICIENT_ROUTE_OBJECTIVE_EVIDENCE")
    if material_conflict:
        preserved.add("MATERIAL_OBJECTIVE_HYPOTHESIS_CONFLICT")
    facts["material_objective_hypothesis_conflict"] = material_conflict
    facts["material_objective_signature_count"] = len(material_signatures)
    facts["wait_boundary_reasons"] = [
        reason for reason in _v4.WAIT_BOUNDARY_REASON_ENUM if reason in preserved
    ]
    return facts


def causal_link_audit(packet: Mapping[str, Any]) -> list[str]:
    """Return objective graph errors without reading identity or outcomes."""

    errors: list[str] = []
    evidence = {
        str(row.get("ref")): row
        for row in packet.get("evidence") or []
        if isinstance(row, dict) and row.get("ref")
    }
    hypotheses = (packet.get("objective_facts") or {}).get("scenario_hypotheses") or {}
    for scenario, rows in hypotheses.items():
        for hypothesis in rows or []:
            relation = evidence.get(str(hypothesis.get("relation_ref") or "")) or {}
            relation_values = relation.get("values") or {}
            attack = evidence.get(str(relation_values.get("current_attack_ref") or "")) or {}
            attack_values = attack.get("values") or {}
            episode_ref = hypothesis.get("episode_stop_ref")
            campaign_ref = hypothesis.get("campaign_stop_ref")
            if episode_ref:
                episode = evidence.get(str(episode_ref)) or {}
                episode_values = episode.get("values") or {}
                if (
                    attack.get("kind") != "CAUSAL_CONTROL_ATTACK"
                    or attack_values.get("defense_result") != "ESTABLISHED"
                    or episode.get("kind") != "STOP_CANDIDATE"
                    or episode_values.get("defense_ref") != attack_values.get("defense_id")
                    or episode_values.get("scale") != attack_values.get("scale")
                    or episode_values.get("control_pivot_ref")
                    != attack_values.get("control_pivot_ref")
                    or episode_values.get("established_on")
                    != attack_values.get("confirmed_on")
                ):
                    errors.append(f"{scenario}: episode stop is not the current attack defense")
            if campaign_ref and scenario in {
                "MATURE_TREND_PULLBACK",
                "MACRO_COPY_RESONANCE",
            }:
                parent = evidence.get(str(relation_values.get("parent_anchor_ref") or "")) or {}
                parent_values = parent.get("values") or {}
                campaign = evidence.get(str(campaign_ref)) or {}
                campaign_values = campaign.get("values") or {}
                relation_campaign = relation_values.get("campaign_id")
                if (
                    parent.get("kind") != "ANCHOR_CANDIDATE"
                    or campaign.get("kind") != "STOP_CANDIDATE"
                    or parent_values.get("campaign_id") != relation_campaign
                    or campaign_values.get("campaign_id") != relation_campaign
                    or parent_values.get("scale") != relation_values.get("parent_scale")
                    or campaign_values.get("scale") != relation_values.get("parent_scale")
                ):
                    errors.append(f"{scenario}: campaign stop is not the parent campaign defense")
            if campaign_ref and scenario in {
                "FRESH_Q1_EXPANSION",
                "BEAR_REVERSAL_LEFT_RIGHT",
            } and campaign_ref != episode_ref:
                errors.append(f"{scenario}: probe/fresh campaign stop differs from episode stop")
    return errors


def assert_anonymous_and_causal(packet: dict[str, Any], *, as_of: str) -> None:
    _ORIGINAL_ASSERT_ANONYMOUS_AND_CAUSAL(packet, as_of=as_of)
    errors = causal_link_audit(packet)
    if errors:
        raise _v4.AtomicPacketError("; ".join(sorted(set(errors))))


def activate() -> None:
    """Install the scoped V6 graph corrections into the pinned V4 engine."""

    assert_base_builder_frozen()
    if _v4.BUILDER_VERSION == BUILDER_VERSION:
        return
    if _v4.BUILDER_VERSION not in {
        "hybrid-v3-atomic-packets-v4",
        BASE_BUILDER_VERSION,
    }:
        raise RuntimeError("unexpected active packet builder version")
    _v5.activate()
    _v4._anchor_candidates = _anchor_candidates
    _v4._scenario_hypotheses = _scenario_hypotheses
    _v4._objective_facts = _objective_facts
    _v4.assert_anonymous_and_causal = assert_anonymous_and_causal
    _v4.BUILDER_VERSION = BUILDER_VERSION
    _v4.BUILDER_STATUS = BUILDER_STATUS
    _v4.__file__ = str(_WRAPPER_FILE)


def __getattr__(name: str) -> Any:
    return getattr(_v4, name)


def main() -> int:
    activate()
    return int(_v4._main())


if __name__ == "__main__":
    raise SystemExit(main())
