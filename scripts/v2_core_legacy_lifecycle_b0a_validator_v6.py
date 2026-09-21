"""Validate and route the R6 B0A lifecycle-only legacy alignment candidate."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


ATOM_ROLE = {
    "active_large_down_still_controls": "ACTIVE_DOWN_CONTROL",
    "current_up_replaced_down_control": "UP_CONTROL_TRANSFER",
    "completed_up_parent_controls_current_correction": "COMPLETED_UP_PARENT",
    "current_is_first_independent_copy": "CURRENT_COPY",
    "mature_success_preexists_current_episode": "PREEXISTING_MATURE_SEQUENCE",
    "long_trend_habit_preexists_current_episode": "PREEXISTING_LONG_TREND_HABIT",
    "current_up_is_fresh_forming_anchor": "CURRENT_FRESH_UP_ANCHOR",
}

ATOM_SUBJECT_DIRECTION = {
    "active_large_down_still_controls": "DOWN",
    "current_up_replaced_down_control": "UP",
    "completed_up_parent_controls_current_correction": "UP",
    "current_is_first_independent_copy": "UP",
    "mature_success_preexists_current_episode": "UP",
    "long_trend_habit_preexists_current_episode": "UP",
    "current_up_is_fresh_forming_anchor": "UP",
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _iso_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _packet_maps(packet: dict[str, Any]) -> tuple[
    dict[str, dict[str, Any]], dict[str, str], set[str], list[str]
]:
    errors: list[str] = []
    candidate_rows = packet.get("candidate_pool")
    option_rows = packet.get("candidate_evidence_options")
    if not isinstance(candidate_rows, list) or not isinstance(option_rows, list):
        return {}, {}, set(), ["packet candidate pool or evidence options missing"]

    candidates: dict[str, dict[str, Any]] = {}
    for row in candidate_rows:
        if not isinstance(row, dict) or not isinstance(row.get("candidate_id"), str):
            errors.append("packet contains malformed candidate")
            continue
        candidate_id = row["candidate_id"]
        if candidate_id in candidates:
            errors.append(f"packet duplicate candidate: {candidate_id}")
        candidates[candidate_id] = row

    option_by_candidate: dict[str, str] = {}
    fixed_refs: set[str] = set()
    for row in option_rows:
        if not isinstance(row, dict):
            errors.append("packet contains malformed evidence option")
            continue
        candidate_id = row.get("candidate_id")
        option_id = row.get("evidence_option_id")
        if not isinstance(candidate_id, str) or not isinstance(option_id, str):
            errors.append("packet evidence option lacks candidate or option id")
            continue
        if candidate_id in option_by_candidate:
            errors.append(f"packet duplicate evidence option for candidate: {candidate_id}")
        option_by_candidate[candidate_id] = option_id
        refs = row.get("source_evidence_refs", [])
        if isinstance(refs, list):
            fixed_refs.update(str(ref) for ref in refs)

    for row in packet.get("proxy_evidence", []):
        if isinstance(row, dict) and isinstance(row.get("ref"), str):
            fixed_refs.add(row["ref"])

    if set(candidates) != set(option_by_candidate):
        errors.append("packet candidate/evidence-option sets differ")
    for candidate_id, candidate in candidates.items():
        if candidate.get("evidence_option_id") != option_by_candidate.get(candidate_id):
            errors.append(f"packet candidate option mismatch: {candidate_id}")
    return candidates, option_by_candidate, fixed_refs, errors


def _validate_binding(
    *,
    label: str,
    candidate_id: Any,
    option_id: Any,
    candidates: dict[str, dict[str, Any]],
    option_by_candidate: dict[str, str],
    errors: list[str],
) -> dict[str, Any] | None:
    candidate_text = str(candidate_id)
    option_text = str(option_id)
    if candidate_text == "NONE" or option_text == "NONE":
        if candidate_text != "NONE" or option_text != "NONE":
            errors.append(f"{label}: candidate and option must both be NONE or both be fixed")
        return None
    candidate = candidates.get(candidate_text)
    if candidate is None:
        errors.append(f"{label}: candidate not in packet")
        return None
    if option_by_candidate.get(candidate_text) != option_text:
        errors.append(f"{label}: evidence option does not match subject candidate")
    return candidate


def _validate_refs(
    *, label: str, refs: Any, fixed_refs: set[str], errors: list[str]
) -> None:
    if not isinstance(refs, list):
        errors.append(f"{label}: supporting refs must be a list")
        return
    for ref in refs:
        if str(ref) not in fixed_refs:
            errors.append(f"{label}: evidence ref not in packet: {ref}")


def _value_at_path(document: dict[str, Any], dotted_path: str) -> Any:
    value: Any = document
    for part in dotted_path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def derive_program_route(
    *, response: dict[str, Any], truth_table: dict[str, Any]
) -> tuple[str, str, list[str], list[str]]:
    """Return scenario, route status, reasons, and matching profiles."""

    lifecycle = response["lifecycle_atoms"]
    expected_atoms = set(truth_table["required_atom_names"])
    if set(lifecycle) != expected_atoms:
        return (
            "UNRESOLVED_NO_TRADE",
            "UNRESOLVED",
            ["TRUTH_TABLE_ATOM_SET_MISMATCH"],
            [],
        )

    matches: list[str] = []
    for route in truth_table["routes"]:
        atoms_match = all(
            lifecycle[name]["judgement"] == expected
            for name, expected in route["required_atoms"].items()
        )
        proofs_match = all(
            _value_at_path(response, path) == expected
            for path, expected in route["required_proofs"].items()
        )
        if atoms_match and proofs_match:
            matches.append(route["scenario_family"])

    if len(matches) == 1:
        return matches[0], "ROUTED", [f"UNIQUE_{matches[0]}_PROFILE_MATCH"], matches
    if len(matches) > 1:
        return (
            "UNRESOLVED_NO_TRADE",
            "UNRESOLVED",
            ["MULTIPLE_ROUTE_PROFILES_MATCH"],
            matches,
        )

    values = {name: atom["judgement"] for name, atom in lifecycle.items()}
    reasons: list[str] = []
    if response["control_transfer_proof"]["result"] == "UNRESOLVED":
        reasons.append("CONTROL_TRANSFER_UNRESOLVED")
    if "UNKNOWN" in values.values():
        reasons.append("REQUIRED_ATOM_UNKNOWN")
    if (
        values["mature_success_preexists_current_episode"] == "PASS"
        and response["maturity_proof"]["status"] != "PASS"
    ):
        reasons.append("MATURITY_PROOF_INCOMPLETE_OR_CONFLICTING")
    if not reasons:
        reasons.append("NO_ROUTE_PROFILE_MATCH")
    return "UNRESOLVED_NO_TRADE", "UNRESOLVED", reasons, []


def validate_response(
    *,
    response: dict[str, Any],
    schema: dict[str, Any],
    packet: dict[str, Any],
    truth_table: dict[str, Any],
) -> dict[str, Any]:
    errors = [
        f"SCHEMA:{'/'.join(str(part) for part in error.absolute_path)}:{error.message}"
        for error in sorted(
            Draft202012Validator(schema).iter_errors(response),
            key=lambda item: list(item.path),
        )
    ]
    candidates, option_by_candidate, fixed_refs, packet_errors = _packet_maps(packet)
    errors.extend(packet_errors)

    required_atoms = truth_table.get("required_atom_names")
    routes = truth_table.get("routes")
    if truth_table.get("input_schema_version") != response.get("schema_version"):
        errors.append("truth table input schema version mismatch")
    if not isinstance(required_atoms, list) or set(required_atoms) != set(ATOM_ROLE):
        errors.append("truth table required atom set mismatch")
    if not isinstance(routes, list) or not routes:
        errors.append("truth table routes missing")
    elif len({row.get("scenario_family") for row in routes}) != len(routes):
        errors.append("truth table contains duplicate scenario routes")
    if truth_table.get("no_trade_permission_at_this_stage") is not True:
        errors.append("truth table must deny trade permission at B0A")

    lifecycle = response.get("lifecycle_atoms")
    if not isinstance(lifecycle, dict):
        return {"status": "INVALID", "errors": sorted(set(errors or ["lifecycle atoms missing"]))}

    bound_atoms: dict[str, tuple[dict[str, Any] | None, dict[str, Any] | None]] = {}
    for atom_name, expected_role in ATOM_ROLE.items():
        atom = lifecycle.get(atom_name)
        if not isinstance(atom, dict):
            errors.append(f"{atom_name}: atom missing")
            continue
        if atom.get("evaluated_role") != expected_role:
            errors.append(f"{atom_name}: evaluated role mismatch")
        subject = _validate_binding(
            label=f"{atom_name}:subject",
            candidate_id=atom.get("subject_candidate_id"),
            option_id=atom.get("evidence_option_id"),
            candidates=candidates,
            option_by_candidate=option_by_candidate,
            errors=errors,
        )
        relation = _validate_binding(
            label=f"{atom_name}:relation",
            candidate_id=atom.get("relation_candidate_id"),
            option_id=atom.get("relation_evidence_option_id"),
            candidates=candidates,
            option_by_candidate=option_by_candidate,
            errors=errors,
        )
        bound_atoms[atom_name] = (subject, relation)
        judgement = atom.get("judgement")
        if judgement in {"PASS", "FAIL"} and subject is None:
            errors.append(f"{atom_name}: PASS/FAIL requires an evaluated subject candidate")
        if subject is not None and subject.get("direction") != ATOM_SUBJECT_DIRECTION[atom_name]:
            errors.append(f"{atom_name}: subject direction mismatch")
        _validate_refs(
            label=atom_name,
            refs=atom.get("supporting_packet_evidence_refs"),
            fixed_refs=fixed_refs,
            errors=errors,
        )

    if lifecycle.get("completed_up_parent_controls_current_correction", {}).get("judgement") == "PASS":
        parent, correction = bound_atoms.get(
            "completed_up_parent_controls_current_correction", (None, None)
        )
        if parent is None or parent.get("status") != "CONFIRMED":
            errors.append("completed parent PASS requires a CONFIRMED UP subject")
        if correction is None or correction.get("direction") != "DOWN":
            errors.append("completed parent PASS requires a related DOWN correction candidate")

    if lifecycle.get("current_is_first_independent_copy", {}).get("judgement") == "PASS":
        current_copy, parent = bound_atoms.get("current_is_first_independent_copy", (None, None))
        if parent is None or parent.get("direction") != "UP" or parent.get("status") != "CONFIRMED":
            errors.append("first copy PASS requires a related CONFIRMED UP parent")
        if current_copy is not None and parent is not None and current_copy is parent:
            errors.append("first copy subject and parent must be different candidates")

    if lifecycle.get("current_up_is_fresh_forming_anchor", {}).get("judgement") == "PASS":
        fresh_up, prior_down = bound_atoms.get("current_up_is_fresh_forming_anchor", (None, None))
        if fresh_up is None or fresh_up.get("status") != "FORMING":
            errors.append("fresh anchor PASS requires a FORMING UP subject")
        if prior_down is None or prior_down.get("direction") != "DOWN":
            errors.append("fresh anchor PASS requires a related DOWN control candidate")

    if lifecycle.get("current_up_replaced_down_control", {}).get("judgement") in {"PASS", "FAIL"}:
        _, compared_down = bound_atoms.get("current_up_replaced_down_control", (None, None))
        if compared_down is None or compared_down.get("direction") != "DOWN":
            errors.append("control transfer PASS/FAIL requires a related DOWN candidate")

    control = response.get("control_transfer_proof")
    if not isinstance(control, dict):
        errors.append("control transfer proof missing")
    else:
        down = _validate_binding(
            label="control_transfer_proof:down",
            candidate_id=control.get("down_candidate_id"),
            option_id=control.get("down_evidence_option_id"),
            candidates=candidates,
            option_by_candidate=option_by_candidate,
            errors=errors,
        )
        up = _validate_binding(
            label="control_transfer_proof:up",
            candidate_id=control.get("up_candidate_id"),
            option_id=control.get("up_evidence_option_id"),
            candidates=candidates,
            option_by_candidate=option_by_candidate,
            errors=errors,
        )
        _validate_refs(
            label="control_transfer_proof",
            refs=control.get("supporting_packet_evidence_refs"),
            fixed_refs=fixed_refs,
            errors=errors,
        )
        result = control.get("result")
        if result in {"DOWN_STILL_CONTROLS", "UP_REPLACED_DOWN"}:
            if down is None or up is None:
                errors.append("resolved control transfer requires fixed DOWN and UP candidates")
            else:
                if down.get("direction") != "DOWN":
                    errors.append("control transfer DOWN candidate has wrong direction")
                if up.get("direction") != "UP":
                    errors.append("control transfer UP candidate has wrong direction")
                if down is up:
                    errors.append("control transfer candidates must be distinct")
        active_value = lifecycle.get("active_large_down_still_controls", {}).get("judgement")
        replaced_value = lifecycle.get("current_up_replaced_down_control", {}).get("judgement")
        expected_control = (
            "DOWN_STILL_CONTROLS"
            if (active_value, replaced_value) == ("PASS", "FAIL")
            else "UP_REPLACED_DOWN"
            if (active_value, replaced_value) == ("FAIL", "PASS")
            else "UNRESOLVED"
        )
        if result != expected_control:
            errors.append("control transfer proof conflicts with control atoms")

    maturity = response.get("maturity_proof")
    if not isinstance(maturity, dict):
        errors.append("maturity proof missing")
    else:
        proof_candidates: dict[str, dict[str, Any] | None] = {}
        for role in ("parent", "correction", "successful_copy", "current_episode"):
            proof_candidates[role] = _validate_binding(
                label=f"maturity_proof:{role}",
                candidate_id=maturity.get(f"{role}_candidate_id"),
                option_id=maturity.get(f"{role}_evidence_option_id"),
                candidates=candidates,
                option_by_candidate=option_by_candidate,
                errors=errors,
            )
        _validate_refs(
            label="maturity_proof",
            refs=maturity.get("supporting_packet_evidence_refs"),
            fixed_refs=fixed_refs,
            errors=errors,
        )
        mature_value = lifecycle.get("mature_success_preexists_current_episode", {}).get(
            "judgement"
        )
        if maturity.get("status") != mature_value:
            errors.append("maturity proof status conflicts with mature-success atom")
        if maturity.get("status") == "UNKNOWN" and maturity.get(
            "sequence_preexists_current_episode"
        ) != "UNKNOWN":
            errors.append("UNKNOWN maturity proof requires UNKNOWN sequence result")
        if maturity.get("status") == "PASS":
            if maturity.get("sequence_preexists_current_episode") != "PASS":
                errors.append("maturity PASS requires preexisting sequence PASS")
            if any(candidate is None for candidate in proof_candidates.values()):
                errors.append("maturity PASS requires all four fixed candidates")
            else:
                parent = proof_candidates["parent"]
                correction = proof_candidates["correction"]
                successful_copy = proof_candidates["successful_copy"]
                current_episode = proof_candidates["current_episode"]
                assert parent is not None
                assert correction is not None
                assert successful_copy is not None
                assert current_episode is not None
                expected_directions = {
                    "parent": "UP",
                    "correction": "DOWN",
                    "successful_copy": "UP",
                    "current_episode": "UP",
                }
                for role, expected_direction in expected_directions.items():
                    if proof_candidates[role].get("direction") != expected_direction:  # type: ignore[union-attr]
                        errors.append(f"maturity proof {role} direction mismatch")
                for role in ("parent", "correction", "successful_copy"):
                    if proof_candidates[role].get("status") != "CONFIRMED":  # type: ignore[union-attr]
                        errors.append(f"maturity proof {role} must be CONFIRMED")
                identifiers = [candidate["candidate_id"] for candidate in proof_candidates.values()]
                if len(set(identifiers)) != 4:
                    errors.append("maturity proof roles must use four distinct candidates")
                starts = [_iso_date(candidate.get("start_date")) for candidate in proof_candidates.values()]
                if any(value is None for value in starts) or not all(
                    left < right for left, right in zip(starts, starts[1:])  # type: ignore[arg-type]
                ):
                    errors.append("maturity proof candidates are not in parent-correction-copy-episode order")
                copy_end = _iso_date(successful_copy.get("confirmed_end_date"))
                episode_start = _iso_date(current_episode.get("start_date"))
                if copy_end is None or episode_start is None or copy_end >= episode_start:
                    errors.append("successful copy must complete before current episode starts")

    if errors:
        return {"status": "INVALID", "errors": sorted(set(errors))}

    scenario, route_status, route_reasons, matching_profiles = derive_program_route(
        response=response, truth_table=truth_table
    )
    return {
        "status": "VALID",
        "errors": [],
        "program_derived_scenario_family": scenario,
        "route_status": route_status,
        "route_reasons": route_reasons,
        "matching_route_profiles": matching_profiles,
        "atom_judgements": {
            name: response["lifecycle_atoms"][name]["judgement"] for name in ATOM_ROLE
        },
        "validator_contract": {
            "ai_output_contains_scenario": False,
            "ai_selects_unique_working_anchor": False,
            "program_invents_course_atoms": False,
            "trade_permission_granted": False,
            "negative_atoms_may_cite_rejected_subjects": True,
            "maturity_requires_preepisode_sequence": True,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--truth-table", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    result = validate_response(
        response=load_json(args.response),
        schema=load_json(args.schema),
        packet=load_json(args.packet),
        truth_table=load_json(args.truth_table),
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if result["status"] == "VALID" else 2


if __name__ == "__main__":
    raise SystemExit(main())
