"""V4-S1 policy: separate mature campaign age from local entry exhaustion.

V1/V2/V3 remain byte-pinned and untouched.  The AI-visible atomic contract is
also unchanged.  Only MATURE_TREND_PULLBACK uses a local-entry stage that does
not infer lateness from the campaign generation alone.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    from . import hybrid_v3_atomic_policy_v2 as _v2
    from . import hybrid_v3_atomic_policy_v3 as _v3
except ImportError:  # pragma: no cover - direct script import
    from scripts import hybrid_v3_atomic_policy_v2 as _v2
    from scripts import hybrid_v3_atomic_policy_v3 as _v3


ROOT = Path(__file__).resolve().parents[1]
POLICY_VERSION = "hybrid-v4-s1-atomic-policy-v1"
POLICY_STATUS = "DRAFT_FOR_OUTCOME_BLIND_RESEARCH"
RULES_PATH = ROOT / "config/enlightenment_ai_rules_v4_s1.json"
EXPECTED_SHA256 = {
    "scripts/hybrid_v3_atomic_policy_v2.py": "b7504b8a1414dc3baab17e047f1827e96c3fa2fdc62b3ce2e6845f18a5cb40b9",
    "scripts/hybrid_v3_atomic_policy_v3.py": "78219086e8f1d5e3f3fc1be977dbb49e3ba3b232ee9c96bc88097106699d8196",
    "config/enlightenment_ai_rules_v3.json": "4e78b5ddeea93d6469eca1fbd6e5309679bfdfc4193f1358cfde09789b201ca6",
    "config/hybrid_semantic_prompt_v3.md": "43bf85d3110c551bbe957d3cc90c78cf50bc8a2bb06cb67677b55f8c2f9cb707",
    "config/hybrid_atomic_semantics_v2.schema.json": "3c4e50608f3384eba2fc1f8464443008988c318ac04b95dca5b8e04700c4362f",
    "config/enlightenment_ai_rules_v4_s1.json": "91760dcdc1b74a5433b57673ed5c042b2627e5f154bc1deb30423bdbb86f3bcc",
}

TARGET_SCENARIO = "MATURE_TREND_PULLBACK"
TARGET_ROUTE = "V2_CORE"
WEAKNESS_ATOMS = (
    "EXH_ATTACK_SHORTENING",
    "EXH_SLOPE_DECAY",
    "EXH_PRICE_VOLUME_DIVERGENCE",
    "EXH_FAILED_CONTINUATION",
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@lru_cache(maxsize=1)
def assert_frozen_ancestors() -> None:
    for relative, expected in EXPECTED_SHA256.items():
        path = ROOT / relative
        if not path.is_file() or _file_sha256(path) != expected:
            raise RuntimeError(f"frozen V4-S1 component changed: {relative}")
    if _v2.POLICY_VERSION != "hybrid-v3-atomic-policy-v2":
        raise RuntimeError("unexpected V2 base policy version")
    if _v3.POLICY_VERSION != "hybrid-v3-atomic-policy-v3":
        raise RuntimeError("unexpected V3 adapter policy version")


def validate_atomic(packet: dict[str, Any], semantic: dict[str, Any]) -> list[str]:
    """Use the V3 validator once; do not re-enter the V2-only validator."""

    assert_frozen_ancestors()
    return _v3.validate_atomic(packet, semantic)


def _candidate_result(
    semantic: dict[str, Any], group: str, subject_ref: Any, question_id: str
) -> str:
    return _v2._candidate_result(
        _v2._answer_indexes(semantic), group, subject_ref, question_id
    )


def derive_campaign_maturity(
    semantic: dict[str, Any], hypothesis: Mapping[str, Any]
) -> str:
    """Classify the large campaign without deciding local entry timing."""

    repeated = _candidate_result(
        semantic,
        "relation_candidates",
        hypothesis.get("relation_ref"),
        "LONG_CAMPAIGN_REPEATED_SUCCESS",
    )
    if repeated == "FAIL":
        return "NOT_MATURE"
    if repeated == "PASS":
        return "MATURE_CONFIRMED"
    return "MATURE_UNRESOLVED"


def derive_local_entry_stage(
    semantic: dict[str, Any], hypothesis: Mapping[str, Any]
) -> str:
    """Evaluate only the current relaunch location, independent of generation."""

    relation_ref = hypothesis.get("relation_ref")

    def result(question_id: str) -> str:
        return _candidate_result(
            semantic, "relation_candidates", relation_ref, question_id
        )

    weak = [result(question_id) for question_id in WEAKNESS_ATOMS]
    time_space = result("EXH_TIME_SPACE_EXHAUSTION")
    remaining = result("LOCATION_REMAINING_SPACE_ADEQUATE")
    not_extended = result("LOCATION_NOT_EXTENDED_FROM_ORIGIN")

    # Strong terminal evidence has priority and cannot be offset by good atoms.
    if time_space == "PASS" or remaining == "FAIL":
        return "EXHAUSTED"
    # A confirmed failure to continue is itself a blocking structural fact,
    # not one ordinary warning that can be offset by three quiet atoms.
    if result("EXH_FAILED_CONTINUATION") == "PASS":
        return "LATE"
    if weak.count("PASS") >= 2 or not_extended == "FAIL":
        return "LATE"

    complete_context = (
        time_space == "FAIL"
        and remaining == "PASS"
        and not_extended == "PASS"
        and all(value in {"PASS", "FAIL"} for value in weak)
    )
    if complete_context and weak.count("PASS") == 0:
        return "EARLY"
    if complete_context and weak.count("PASS") == 1:
        return "MIDDLE"
    return "UNRESOLVED"


def _generation_risk(hypothesis: Mapping[str, Any]) -> str:
    attack_number = hypothesis.get("same_direction_attack_number")
    return (
        "DEGRADED_LATE_GENERATION_OR_ATTACK"
        if (
            hypothesis.get("taiji_generation")
            in {"COPY_LEG_5", "LATER_GENERATION"}
            or (
                isinstance(attack_number, int)
                and not isinstance(attack_number, bool)
                and attack_number >= 3
            )
        )
        else "STANDARD_GENERATION"
    )


def _risk_disclosure(
    semantic: dict[str, Any], hypothesis: Mapping[str, Any]
) -> dict[str, Any]:
    relation_ref = hypothesis.get("relation_ref")

    def result(question_id: str) -> str:
        return _candidate_result(
            semantic, "relation_candidates", relation_ref, question_id
        )

    generation = hypothesis.get("taiji_generation")
    attack_number = hypothesis.get("same_direction_attack_number")
    grade = _generation_risk(hypothesis)
    required = (
        ["REMAINING_SPACE", "CHASE_RISK", "COPY_FAILURE_RISK"]
        if grade == "DEGRADED_LATE_GENERATION_OR_ATTACK"
        else []
    )
    return {
        "setup_grade": grade,
        "late_generation": generation in {"COPY_LEG_5", "LATER_GENERATION"},
        "third_or_later_attack": (
            isinstance(attack_number, int)
            and not isinstance(attack_number, bool)
            and attack_number >= 3
        ),
        "required_disclosures": required,
        "remaining_space": result("LOCATION_REMAINING_SPACE_ADEQUATE"),
        "not_extended_from_origin": result(
            "LOCATION_NOT_EXTENDED_FROM_ORIGIN"
        ),
        "attack_shortening": result("EXH_ATTACK_SHORTENING"),
        "slope_decay": result("EXH_SLOPE_DECAY"),
        "price_volume_divergence": result("EXH_PRICE_VOLUME_DIVERGENCE"),
        "failed_continuation": result("EXH_FAILED_CONTINUATION"),
        "time_space_exhaustion": result("EXH_TIME_SPACE_EXHAUSTION"),
    }


def build_gate_matrix(
    semantic: dict[str, Any],
    objective: dict[str, Any],
    base_derived: dict[str, Any] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Reuse V3 gates and replace only mature scenario stage derivation."""

    matrix = deepcopy(_v2.build_gate_matrix(semantic, objective, base_derived))
    for evaluation in matrix.get(TARGET_SCENARIO, {}).values():
        hypothesis = evaluation["hypothesis"]
        legacy_stage = evaluation["derived"]["stage"]
        local_stage = derive_local_entry_stage(semantic, hypothesis)
        evaluation["derived"].update(
            {
                "legacy_generation_stage": legacy_stage,
                "stage": local_stage,
                "local_entry_stage": local_stage,
                "campaign_maturity": derive_campaign_maturity(
                    semantic, hypothesis
                ),
                "generation_risk": _generation_risk(hypothesis),
                "risk_disclosure": _risk_disclosure(semantic, hypothesis),
            }
        )
        maturity = evaluation["derived"]["campaign_maturity"]
        evaluation["scenario_boundary"] = {
            "MATURE_CONFIRMED": "PASS",
            "MATURE_UNRESOLVED": "UNKNOWN",
            "NOT_MATURE": "FAIL",
        }[maturity]
    return matrix


def _common_hard_guards(
    scenario: str,
    semantic: dict[str, Any],
    objective: dict[str, Any],
    evaluation: dict[str, Any],
) -> dict[str, str]:
    guards = _v2._common_hard_guards(
        scenario, semantic, objective, evaluation
    )
    if scenario != TARGET_SCENARIO:
        return guards
    local_result = guards.pop("NOT_LATE_OR_EXHAUSTED")
    return {
        **guards,
        "CAMPAIGN_MATURITY_CONFIRMED": (
            "PASS"
            if evaluation["derived"]["campaign_maturity"] == "MATURE_CONFIRMED"
            else (
                "UNKNOWN"
                if evaluation["derived"]["campaign_maturity"]
                == "MATURE_UNRESOLVED"
                else "FAIL"
            )
        ),
        "LOCAL_ENTRY_NOT_LATE_OR_EXHAUSTED": local_result,
    }


def _decision_signature(decision: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(
        json.dumps(decision.get(field), ensure_ascii=False, sort_keys=True)
        for field in (
            "permission",
            "route",
            "scenario",
            "unknown_gate",
            "action_signature",
            "reason_codes",
        )
    )


def reduce_atomic_v4_s1(
    packet: dict[str, Any], semantic: dict[str, Any]
) -> dict[str, Any]:
    """Deterministically reduce one anonymous stock-day under V4-S1."""

    errors = validate_atomic(packet, semantic)
    if errors:
        invalid_code = (
            "INVALID_PACKET"
            if any(error.startswith("packet:") for error in errors)
            else "INVALID_AI_OUTPUT"
        )
        return {
            "permission": "WAIT",
            "route": "NO_TRADE",
            "reason_codes": [invalid_code],
            "validation_errors": errors,
        }

    objective = packet.get("objective_facts") or {}
    base_derived = {
        "quadrants": _v2.derive_quadrants(semantic),
        "scale_relationship": _v2.derive_scale_relationship(semantic),
    }
    matrix = build_gate_matrix(semantic, objective, base_derived)
    primary_scenario = _v2.derive_primary_scenario(matrix)
    derived = {
        **base_derived,
        "primary_scenario": primary_scenario,
        "hypothesis_states": {
            scenario: {
                hypothesis_id: evaluation["derived"]
                for hypothesis_id, evaluation in rows.items()
            }
            for scenario, rows in matrix.items()
        },
    }
    if primary_scenario in _v2.SCENARIOS:
        primary_states = [
            row["derived"] for row in matrix[primary_scenario].values()
        ]
        for field in ("stage", "left_right_phase"):
            values = {state[field] for state in primary_states}
            derived[field] = (
                next(iter(values)) if len(values) == 1 else "UNRESOLVED"
            )
        if primary_scenario == TARGET_SCENARIO:
            for field in (
                "campaign_maturity",
                "local_entry_stage",
                "generation_risk",
                "risk_disclosure",
            ):
                encoded = {
                    json.dumps(state[field], ensure_ascii=False, sort_keys=True)
                    for state in primary_states
                }
                derived[field] = (
                    deepcopy(primary_states[0][field])
                    if len(encoded) == 1
                    else "UNRESOLVED"
                )

    invalidated = _v2._normalize_verdict(
        objective.get("parent_campaign_invalidated")
    )
    alert = _v2._normalize_verdict(objective.get("macro_defense_alert"))
    if alert == invalidated == "PASS":
        return {
            "permission": "REMOVE",
            "route": "NO_TRADE",
            "scenario": derived["primary_scenario"],
            "reason_codes": ["LARGE_CAMPAIGN_INVALIDATED"],
            "derived_structure": derived,
            "gate_matrix": matrix,
        }

    guards_by_scenario: dict[str, dict[str, dict[str, str]]] = {
        scenario: {} for scenario in _v2.SCENARIOS
    }
    hypotheses: list[dict[str, Any]] = []
    incomplete_signatures: list[dict[str, Any]] = []
    for scenario in _v2.SCENARIOS:
        for hypothesis_id, evaluation in matrix.get(scenario, {}).items():
            guards = _common_hard_guards(
                scenario, semantic, objective, evaluation
            )
            guards_by_scenario[scenario][hypothesis_id] = guards
            route, unknown = _v2._eligible_route(
                scenario, semantic, objective, evaluation, guards
            )
            if not route:
                continue
            signature, missing = _v2._signature(
                route, evaluation["hypothesis"], objective
            )
            row = {
                "scenario": scenario,
                "hypothesis_id": hypothesis_id,
                "route": route,
                "unknown_gate": unknown,
            }
            if signature is None:
                row["missing_signature_fields"] = missing
                incomplete_signatures.append(row)
            else:
                row["action_signature"] = signature
                hypotheses.append(row)

    unique_signatures = {
        json.dumps(
            row["action_signature"], ensure_ascii=False, sort_keys=True
        )
        for row in hypotheses
    }
    if len(unique_signatures) > 1:
        return {
            "permission": "WAIT",
            "route": "NO_TRADE",
            "scenario": derived["primary_scenario"],
            "reason_codes": ["MATERIAL_HYPOTHESIS_CONFLICT"],
            "derived_structure": derived,
            "gate_matrix": matrix,
            "common_hard_guards": guards_by_scenario,
            "hypotheses": hypotheses,
        }
    if incomplete_signatures:
        return {
            "permission": "WAIT",
            "route": "NO_TRADE",
            "scenario": derived["primary_scenario"],
            "reason_codes": ["ACTION_SIGNATURE_INCOMPLETE"],
            "derived_structure": derived,
            "gate_matrix": matrix,
            "common_hard_guards": guards_by_scenario,
            "hypotheses": hypotheses + incomplete_signatures,
        }

    if hypotheses:
        primary = primary_scenario
        if primary in {"UNRESOLVED_NO_TRADE", "NO_TRADE"}:
            return {
                "permission": "WAIT",
                "route": "NO_TRADE",
                "scenario": primary,
                "reason_codes": [
                    "PRIMARY_SCENARIO_UNRESOLVED"
                    if primary == "UNRESOLVED_NO_TRADE"
                    else "PRIMARY_SCENARIO_CONFLICT"
                ],
                "derived_structure": derived,
                "gate_matrix": matrix,
                "common_hard_guards": guards_by_scenario,
                "hypotheses": hypotheses,
            }
        matching = [
            row for row in hypotheses if row["scenario"] == primary
        ]
        if not matching:
            return {
                "permission": "WAIT",
                "route": "NO_TRADE",
                "scenario": primary,
                "reason_codes": ["PRIMARY_SCENARIO_CONFLICT"],
                "derived_structure": derived,
                "gate_matrix": matrix,
                "common_hard_guards": guards_by_scenario,
                "hypotheses": hypotheses,
            }
        selected = min(
            matching,
            key=lambda row: (
                _v2.ROUTE_PRECEDENCE[row["route"]],
                row["hypothesis_id"],
            ),
        )
        selected_state = matrix[selected["scenario"]][
            selected["hypothesis_id"]
        ]["derived"]
        derived["stage"] = selected_state["stage"]
        derived["left_right_phase"] = selected_state["left_right_phase"]
        derived["selected_hypothesis_id"] = selected["hypothesis_id"]
        if selected["scenario"] == TARGET_SCENARIO:
            derived["campaign_maturity"] = selected_state[
                "campaign_maturity"
            ]
            derived["local_entry_stage"] = selected_state[
                "local_entry_stage"
            ]
            derived["generation_risk"] = selected_state["generation_risk"]
            derived["risk_disclosure"] = selected_state["risk_disclosure"]
        if (
            selected["scenario"] != TARGET_SCENARIO
            or selected["route"] != TARGET_ROUTE
        ):
            return {
                "permission": "WAIT",
                "route": "NO_TRADE",
                "scenario": selected["scenario"],
                "reason_codes": ["OUTSIDE_V4_S1_STAGE_SCOPE"],
                "derived_structure": derived,
                "gate_matrix": matrix,
                "common_hard_guards": guards_by_scenario,
                "hypotheses": hypotheses,
                "suppressed_candidate": {
                    "scenario": selected["scenario"],
                    "route": selected["route"],
                    "hypothesis_id": selected["hypothesis_id"],
                },
            }
        action_signature = deepcopy(selected["action_signature"])
        risk_disclosure = selected_state["risk_disclosure"]
        action_signature.update(
            {
                "setup_grade": risk_disclosure["setup_grade"],
                "required_risk_disclosures": risk_disclosure[
                    "required_disclosures"
                ],
            }
        )
        return {
            "permission": "TRADE",
            "route": selected["route"],
            "scenario": selected["scenario"],
            "unknown_gate": selected["unknown_gate"],
            "action_signature": action_signature,
            "reason_codes": [],
            "derived_structure": derived,
            "gate_matrix": matrix,
            "common_hard_guards": guards_by_scenario,
            "hypotheses": hypotheses,
        }

    reason_codes = ["NO_ELIGIBLE_ROUTE"]
    if alert == "UNKNOWN" or invalidated == "UNKNOWN":
        reason_codes.append("CAMPAIGN_INVALIDATION_UNKNOWN")
    return {
        "permission": "WAIT",
        "route": "NO_TRADE",
        "scenario": derived["primary_scenario"],
        "reason_codes": reason_codes,
        "derived_structure": derived,
        "gate_matrix": matrix,
        "common_hard_guards": guards_by_scenario,
        "hypotheses": [],
    }


def stage_permission_v4_s1(decision: Mapping[str, Any]) -> str:
    """Collapse full policy output to the single authorized research route."""

    if decision.get("permission") == "REMOVE":
        return "REMOVE"
    if (
        decision.get("permission") == "TRADE"
        and decision.get("route") == TARGET_ROUTE
        and decision.get("scenario") == TARGET_SCENARIO
    ):
        return "TRADE"
    return "WAIT"


def validate_course_invariants(
    packet: dict[str, Any],
    semantic: dict[str, Any],
    decision: dict[str, Any],
) -> list[str]:
    """Validate deterministic V4-S1 integrity, not semantic gold truth."""

    errors = [
        f"atomic contract invalid: {error}"
        for error in validate_atomic(packet, semantic)
    ]
    if errors:
        return errors
    expected = reduce_atomic_v4_s1(packet, semantic)
    if _decision_signature(decision) != _decision_signature(expected):
        errors.append("decision material signature differs from V4-S1 reducer")
    for field in (
        "primary_scenario",
        "selected_hypothesis_id",
        "stage",
        "left_right_phase",
        "campaign_maturity",
        "local_entry_stage",
        "generation_risk",
        "risk_disclosure",
    ):
        actual = (decision.get("derived_structure") or {}).get(field)
        wanted = (expected.get("derived_structure") or {}).get(field)
        if actual != wanted:
            errors.append(
                f"decision derived field differs from V4-S1 reducer: {field}"
            )
    if stage_permission_v4_s1(decision) == "TRADE":
        derived = decision.get("derived_structure") or {}
        if derived.get("campaign_maturity") != "MATURE_CONFIRMED":
            errors.append("V4-S1 TRADE requires mature campaign confirmation")
        if derived.get("local_entry_stage") not in {"EARLY", "MIDDLE"}:
            errors.append("V4-S1 TRADE requires EARLY or MIDDLE local entry")
        risk_disclosure = derived.get("risk_disclosure") or {}
        signature = decision.get("action_signature") or {}
        if signature.get("setup_grade") != risk_disclosure.get("setup_grade"):
            errors.append("V4-S1 TRADE setup grade differs from risk disclosure")
        if signature.get("required_risk_disclosures") != risk_disclosure.get(
            "required_disclosures"
        ):
            errors.append("V4-S1 TRADE required risk disclosures differ")
        selected_id = derived.get("selected_hypothesis_id")
        gates = (
            (((decision.get("gate_matrix") or {}).get(TARGET_SCENARIO) or {}).get(selected_id) or {}).get("gates")
            or {}
        )
        if not gates or any(row.get("result") != "PASS" for row in gates.values()):
            errors.append("V4-S1 V2_CORE TRADE requires every mature gate PASS")
        guards = (
            (((decision.get("common_hard_guards") or {}).get(TARGET_SCENARIO) or {}).get(selected_id))
            or {}
        )
        if not guards or any(value != "PASS" for value in guards.values()):
            errors.append("V4-S1 TRADE requires every common hard guard PASS")
    return sorted(set(errors))


assert_frozen_ancestors()
