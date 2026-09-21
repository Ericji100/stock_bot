"""FINAL atomic semantic validation and deterministic V1/V2/V3 policy reduction.

The AI output contains atomic PASS/FAIL/UNKNOWN answers only.  All structure labels,
scenario gates and trading permissions are derived here from those answers plus
causal program facts supplied by the event-packet builder.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "config/hybrid_atomic_semantics_v2.schema.json"
V2_RULES_PATH = ROOT / "config/enlightenment_ai_rules_v2.json"
V3_RULES_PATH = ROOT / "config/enlightenment_ai_rules_v3.json"
FROZEN_GATE_PATH = ROOT / "config/hybrid_monitoring_protocol_v1.json"

POLICY_VERSION = "hybrid-v3-atomic-policy-v2"
POLICY_STATUS = "FINAL"

VERDICTS = {"PASS", "FAIL", "UNKNOWN"}
SCENARIOS = (
    "MATURE_TREND_PULLBACK",
    "MACRO_COPY_RESONANCE",
    "BEAR_REVERSAL_LEFT_RIGHT",
    "FRESH_Q1_EXPANSION",
)
ROUTE_PRECEDENCE = {
    "V2_CORE": 0,
    "NEAR_PASS_MACRO_COPY": 1,
    "NEAR_PASS_FRESH_Q1": 2,
    "BEAR_REVERSAL_PROBE": 3,
}
SCENARIO_ROUTE = {
    "MACRO_COPY_RESONANCE": "NEAR_PASS_MACRO_COPY",
    "FRESH_Q1_EXPANSION": "NEAR_PASS_FRESH_Q1",
    "BEAR_REVERSAL_LEFT_RIGHT": "BEAR_REVERSAL_PROBE",
}

# V2 calls 750 visible sessions a preferred full-history target (200 warm-up
# plus 500 structure sessions), not a universal minimum.  Monitoring V2 keeps
# that conservative operational requirement for the three history-dependent
# routes.  FRESH uses a separately disclosed 200-session operational minimum
# because V2 explicitly permits newly listed stocks to be assessed only as a
# separate fresh-anchor cohort.  The builder must additionally prove the
# fresh-specific objective inputs; bar count alone is never sufficient.
ROUTE_REQUIRED_VISIBLE_BARS = {
    "MATURE_TREND_PULLBACK": 750,
    "MACRO_COPY_RESONANCE": 750,
    "BEAR_REVERSAL_LEFT_RIGHT": 750,
    "FRESH_Q1_EXPANSION": 200,
}
DATA_SUFFICIENCY_REASON_CODES = {
    "SUFFICIENT_ROUTE_HISTORY_AND_OBJECTIVE_EVIDENCE",
    "INSUFFICIENT_VISIBLE_BARS",
    "INSUFFICIENT_ROUTE_OBJECTIVE_EVIDENCE",
    "ROUTE_DATA_SUFFICIENCY_UNRESOLVED",
}
COMPARISON_SELECTION_POLICY = "LATEST_TWO_COMPLETED_SAME_DIRECTION_LEGACY_PIVOT_LEGS"
COMPARISON_LEG_FIELDS = {
    "start_date", "end_date", "start_ref", "end_ref", "start_price", "end_price",
    "bar_count", "price_change_pct", "slope_pct_per_bar", "mean_true_range_pct",
    "realized_close_volatility_pct",
}
COMPARISON_WINDOW_FIELDS = {
    "scale", "direction", "selection_policy", "baseline", "current",
    "baseline_available_on", "current_available_on", "available_on",
}


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _walk_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            yield from _walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_keys(nested)


def _schema_error(error: Any) -> str:
    location = ".".join(str(part) for part in error.absolute_path)
    return f"{location}: {error.message}" if location else error.message


def contract_errors() -> list[str]:
    """Verify that FINAL metadata still represents the frozen V2/V3 gates."""

    schema = read_json(SCHEMA_PATH)
    v2 = read_json(V2_RULES_PATH)
    v3 = read_json(V3_RULES_PATH)
    frozen = read_json(FROZEN_GATE_PATH)
    errors: list[str] = []
    matrix = schema.get("x-four-scenario-gate-matrix") or {}
    if set(matrix) != set(v2["scenario_rules"]):
        errors.append("schema scenario set differs from frozen V2 strategy")
    for scenario in SCENARIOS:
        expected = set(frozen["v2_required_semantic_gates"][scenario])
        if set((matrix.get(scenario) or {}).keys()) != expected:
            errors.append(f"{scenario} gate set differs from frozen V2 gate contract")

    for route in ("NEAR_PASS_MACRO_COPY", "NEAR_PASS_FRESH_Q1", "BEAR_REVERSAL_PROBE"):
        current = v3["layers"][route]
        frozen_route = frozen["v3_near_pass"][route]
        if current["hard_pass_gates"] != frozen_route["hard"]:
            errors.append(f"{route} hard gates differ from frozen V3 gate contract")
        if current["soft_gates"] != frozen_route["soft"]:
            errors.append(f"{route} soft gates differ from frozen V3 gate contract")
    if v3["precedence"][:4] != [
        "V2_CORE",
        "NEAR_PASS_MACRO_COPY",
        "NEAR_PASS_FRESH_Q1",
        "BEAR_REVERSAL_PROBE",
    ]:
        errors.append("V3 route precedence differs from reducer contract")
    if (schema.get("x-route-data-sufficiency") or {}).get("required_visible_bars") != ROUTE_REQUIRED_VISIBLE_BARS:
        errors.append("route data sufficiency thresholds differ from reducer contract")
    if (schema.get("x-working-comparison-windows") or {}).get("selection_policy") != COMPARISON_SELECTION_POLICY:
        errors.append("comparison window selection policy differs from reducer contract")
    return errors


def _manifest(packet: dict[str, Any]) -> dict[str, Any]:
    return packet.get("question_manifest") or {}


def expected_question_manifest(packet: dict[str, Any]) -> dict[str, Any]:
    """Derive the exact subject-specific questions from objective hypotheses only."""

    schema = read_json(SCHEMA_PATH)
    groups = schema["x-question-groups"]
    q_group = {
        question_id: group for group, question_ids in groups.items() for question_id in question_ids
    }
    dependencies = schema.get("x-program-atomic-dependencies") or {}
    matrix = schema["x-four-scenario-gate-matrix"]
    common = schema["x-v3-common-hard-guards"]
    required: dict[str, dict[str, set[str]]] = {
        "anchor": {}, "relation": {}, "stop": {},
    }
    global_ids: set[str] = set()

    def add(group: str, subject_ref: Any, question_id: str) -> None:
        if group == "global":
            global_ids.add(question_id)
        elif isinstance(subject_ref, str) and subject_ref not in {"", "UNKNOWN", "UNRESOLVED"}:
            required[group].setdefault(subject_ref, set()).add(question_id)

    scenario_hypotheses = ((packet.get("objective_facts") or {}).get("scenario_hypotheses") or {})
    for scenario in SCENARIOS:
        sources = [
            source
            for gate_sources in (matrix.get(scenario) or {}).values()
            for source in gate_sources
        ]
        sources.extend(source for guard_sources in common.values() for source in guard_sources)
        expanded: list[str] = []
        for source in sources:
            if source.startswith("PROGRAM:"):
                expanded.extend(dependencies.get(source.split(":", 1)[1]) or [])
            else:
                expanded.append(source)
        for hypothesis in scenario_hypotheses.get(scenario) or []:
            if not isinstance(hypothesis, dict):
                continue
            for question_id in expanded:
                group = q_group.get(question_id)
                if group == "anchor":
                    add(group, hypothesis.get("anchor_ref"), question_id)
                elif group == "relation":
                    add(group, hypothesis.get("relation_ref"), question_id)
                elif group == "stop":
                    stop_ref = (
                        hypothesis.get("episode_stop_ref")
                        if question_id == "STOP_BELONGS_TO_CURRENT_EPISODE"
                        else hypothesis.get("campaign_stop_ref")
                    )
                    add(group, stop_ref, question_id)
                elif group == "global":
                    add(group, None, question_id)

    manifest: dict[str, Any] = {}
    for group, output_key in (
        ("anchor", "anchor_candidates"),
        ("relation", "relation_candidates"),
        ("stop", "stop_candidates"),
    ):
        question_order = groups[group]
        manifest[output_key] = [
            {
                "subject_ref": subject_ref,
                "required_question_ids": [
                    question_id for question_id in question_order
                    if question_id in required[group][subject_ref]
                ],
            }
            for subject_ref in sorted(required[group])
        ]
    manifest["global_question_ids"] = [
        question_id for question_id in groups["global"] if question_id in global_ids
    ]
    return manifest


def _evidence_map(packet: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    result: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    rows = packet.get("evidence")
    if not isinstance(rows, list):
        return result, ["packet: evidence must be a flat array"]
    for row in rows:
        if not isinstance(row, dict):
            errors.append("packet: evidence row must be an object")
            continue
        ref = str(row.get("ref") or "")
        if not ref:
            errors.append("evidence row has no ref")
        elif ref in result:
            errors.append(f"duplicate evidence ref: {ref}")
        else:
            result[ref] = row
    return result, errors


def _candidate_rows(semantic: dict[str, Any], group: str) -> list[dict[str, Any]]:
    return list(((semantic.get("candidate_answers") or {}).get(group) or []))


def _iter_verdicts(semantic: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for group in ("anchor_candidates", "relation_candidates", "stop_candidates"):
        for row in _candidate_rows(semantic, group):
            for verdict in (row.get("answers") or {}).values():
                yield verdict
    for verdict in (semantic.get("global_answers") or {}).values():
        yield verdict


def _used_evidence_refs(semantic: dict[str, Any]) -> Iterable[str]:
    for verdict in _iter_verdicts(semantic):
        for field in ("supporting_evidence_refs", "contradicting_evidence_refs"):
            for ref in verdict.get(field) or []:
                yield str(ref)


def _date_after_as_of(value: Any, as_of: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value) > date.fromisoformat(as_of)
    except ValueError:
        return True


def _validate_route_data_and_comparison_windows(
    packet: dict[str, Any], semantic: dict[str, Any], evidence: dict[str, dict[str, Any]]
) -> list[str]:
    """Validate objective route sufficiency and the unique as-of comparison pair."""

    errors: list[str] = []
    objective = packet.get("objective_facts") or {}
    as_of = str(packet.get("as_of") or "")
    actual_visible = objective.get("full_history_visible_bar_count")
    route_rows = objective.get("data_sufficiency_by_route")
    if not isinstance(actual_visible, int) or isinstance(actual_visible, bool) or actual_visible < 0:
        errors.append("packet: full_history_visible_bar_count must be a nonnegative integer")
    if not isinstance(route_rows, dict) or set(route_rows) != set(SCENARIOS):
        errors.append("packet: data_sufficiency_by_route must contain exactly the four frozen scenarios")
    else:
        required_fields = {"status", "required_visible_bars", "actual_visible_bars", "reason_code"}
        for scenario in SCENARIOS:
            row = route_rows.get(scenario)
            if not isinstance(row, dict) or set(row) != required_fields:
                errors.append(f"packet: {scenario} data sufficiency requires exact fields")
                continue
            status = row.get("status")
            required = row.get("required_visible_bars")
            actual = row.get("actual_visible_bars")
            reason = row.get("reason_code")
            if status is not True and status is not False and status is not None:
                errors.append(f"packet: {scenario} data sufficiency status must be true, false or null")
            if required != ROUTE_REQUIRED_VISIBLE_BARS[scenario]:
                errors.append(f"packet: {scenario} required_visible_bars differs from frozen operational minimum")
            if not isinstance(actual, int) or isinstance(actual, bool) or actual < 0:
                errors.append(f"packet: {scenario} actual_visible_bars must be a nonnegative integer")
            elif isinstance(actual_visible, int) and actual != actual_visible:
                errors.append(f"packet: {scenario} actual_visible_bars differs from full history count")
            if reason not in DATA_SUFFICIENCY_REASON_CODES:
                errors.append(f"packet: {scenario} data sufficiency reason_code is invalid")
            if status is True and (not isinstance(actual, int) or actual < ROUTE_REQUIRED_VISIBLE_BARS[scenario]):
                errors.append(f"packet: {scenario} cannot be data sufficient below its operational minimum")
            if status is True and reason != "SUFFICIENT_ROUTE_HISTORY_AND_OBJECTIVE_EVIDENCE":
                errors.append(f"packet: {scenario} sufficient status requires the sufficient reason code")
            if status is False and reason not in {
                "INSUFFICIENT_VISIBLE_BARS", "INSUFFICIENT_ROUTE_OBJECTIVE_EVIDENCE",
            }:
                errors.append(f"packet: {scenario} false status requires an insufficient reason code")
            if status is None and reason != "ROUTE_DATA_SUFFICIENCY_UNRESOLVED":
                errors.append(f"packet: {scenario} null status requires the unresolved reason code")
            required_minimum = ROUTE_REQUIRED_VISIBLE_BARS[scenario]
            if reason == "INSUFFICIENT_VISIBLE_BARS" and isinstance(actual, int) and actual >= required_minimum:
                errors.append(f"packet: {scenario} insufficient-visible-bars reason contradicts the counts")
            if isinstance(actual, int) and actual < required_minimum and (
                status is not False or reason != "INSUFFICIENT_VISIBLE_BARS"
            ):
                errors.append(f"packet: {scenario} below-minimum history must fail as insufficient visible bars")
            scenario_rows = (objective.get("scenario_hypotheses") or {}).get(scenario)
            if status is True and not isinstance(scenario_rows, list):
                errors.append(f"packet: {scenario} sufficient status requires scenario hypotheses")
            elif status is True and not scenario_rows:
                errors.append(f"packet: {scenario} sufficient status cannot have an empty hypothesis set")
            if status is True and scenario == "FRESH_Q1_EXPANSION" and not any(
                isinstance(hypothesis, dict)
                and hypothesis.get("anchor_ref") not in {None, "", "UNKNOWN", "UNRESOLVED"}
                and hypothesis.get("relation_ref") not in {None, "", "UNKNOWN", "UNRESOLVED"}
                and hypothesis.get("episode_stop_ref") not in {None, "", "UNKNOWN", "UNRESOLVED"}
                for hypothesis in (scenario_rows or [])
            ):
                errors.append("packet: FRESH sufficient status requires fresh anchor/relation/stop evidence")

    windows = objective.get("working_comparison_windows")
    if not isinstance(windows, dict) or set(windows) != {"LARGE", "SMALL"}:
        errors.append("packet: working_comparison_windows must contain exactly LARGE and SMALL")
        windows = {}
    ready_window_refs: dict[str, str] = {}
    for scale in ("LARGE", "SMALL"):
        window = windows.get(scale)
        if window is None:
            continue
        if not isinstance(window, dict) or set(window) != COMPARISON_WINDOW_FIELDS:
            errors.append(f"packet: {scale} comparison window requires exact fields")
            continue
        if window.get("scale") != scale:
            errors.append(f"packet: {scale} comparison window scale differs from key")
        if window.get("direction") not in {"UP", "DOWN"}:
            errors.append(f"packet: {scale} comparison window direction is invalid")
        if window.get("selection_policy") != COMPARISON_SELECTION_POLICY:
            errors.append(f"packet: {scale} comparison window selection policy is invalid")
        for label in ("baseline", "current"):
            leg = window.get(label)
            if not isinstance(leg, dict) or set(leg) != COMPARISON_LEG_FIELDS:
                errors.append(f"packet: {scale} {label} comparison leg requires exact fields")
                continue
            start = leg.get("start_date")
            end = leg.get("end_date")
            if _date_after_as_of(start, as_of) or _date_after_as_of(end, as_of):
                errors.append(f"packet: {scale} {label} comparison leg has invalid or future dates")
            else:
                try:
                    if date.fromisoformat(str(start)) > date.fromisoformat(str(end)):
                        errors.append(f"packet: {scale} {label} comparison leg dates are reversed")
                except ValueError:
                    errors.append(f"packet: {scale} {label} comparison leg dates are invalid")
            for ref_field in ("start_ref", "end_ref"):
                if leg.get(ref_field) not in evidence:
                    errors.append(f"packet: {scale} {label} {ref_field} is absent from evidence")
            for price_field in ("start_price", "end_price"):
                value = leg.get(price_field)
                if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                    errors.append(f"packet: {scale} {label} {price_field} must be positive")
            count = leg.get("bar_count")
            if not isinstance(count, int) or isinstance(count, bool) or count < 2:
                errors.append(f"packet: {scale} {label} bar_count must be at least two")
            for metric in (
                "price_change_pct", "slope_pct_per_bar", "mean_true_range_pct",
                "realized_close_volatility_pct",
            ):
                value = leg.get(metric)
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    errors.append(f"packet: {scale} {label} {metric} must be numeric")
            for metric in ("mean_true_range_pct", "realized_close_volatility_pct"):
                value = leg.get(metric)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0:
                    errors.append(f"packet: {scale} {label} {metric} cannot be negative")
        for field in ("baseline_available_on", "current_available_on", "available_on"):
            if _date_after_as_of(window.get(field), as_of):
                errors.append(f"packet: {scale} comparison window {field} is invalid or future")
        matching = [
            ref for ref, row in evidence.items()
            if row.get("kind") == "CAUSAL_COMPARISON_WINDOW"
            and row.get("date") == window.get("available_on")
            and row.get("values") == window
        ]
        if len(matching) != 1:
            errors.append(f"packet: {scale} comparison window requires exactly one matching evidence ref")
        else:
            ready_window_refs[scale] = matching[0]

    # A route claimed sufficient must expose both comparison windows because
    # every V2/V3 route consumes dynamic quadrant and/or direction evidence.
    if isinstance(route_rows, dict) and any(
        isinstance(row, dict) and row.get("status") is True for row in route_rows.values()
    ) and set(ready_window_refs) != {"LARGE", "SMALL"}:
        errors.append("packet: a sufficient route requires ready LARGE and SMALL comparison windows")

    # Without a frozen comparison pair the four comparison atoms cannot be a
    # model PASS/FAIL assertion.  UNKNOWN remains a valid, fail-closed answer.
    globals_ = semantic.get("global_answers") or {}
    for scale in ("LARGE", "SMALL"):
        if scale in ready_window_refs:
            continue
        for question_id in (
            f"{scale}_TREND_STRENGTH_INCREASED", f"{scale}_VOLATILITY_EXPANDED",
        ):
            answer = globals_.get(question_id)
            if isinstance(answer, dict) and answer.get("result") != "UNKNOWN":
                errors.append(f"{question_id} requires UNKNOWN when its comparison window is unavailable")
    return errors


def validate_atomic(packet: dict[str, Any], semantic: dict[str, Any]) -> list[str]:
    """Validate schema plus the cross-document manifest/evidence contract."""

    schema = read_json(SCHEMA_PATH)
    errors = [_schema_error(row) for row in Draft202012Validator(
        schema, format_checker=FormatChecker()
    ).iter_errors(semantic)]
    errors.extend(contract_errors())

    required_packet_fields = {
        "review_id", "anonymous_stock_id", "as_of", "input_packet_sha256",
        "question_manifest_sha256", "evidence_catalog_sha256", "question_manifest",
        "evidence", "objective_facts",
    }
    for field in sorted(required_packet_fields - set(packet)):
        errors.append(f"packet: required top-level field is missing: {field}")
    for field in sorted(set(packet) - required_packet_fields):
        errors.append(f"packet: unexpected top-level field is forbidden: {field}")
    legacy_fields = {"required_question_manifest", "evidence_catalog", "program_facts", "integrity", "scenario_subjects"}
    for field in sorted(legacy_fields.intersection(packet)):
        errors.append(f"packet: legacy top-level field is forbidden: {field}")

    for field in (
        "review_id",
        "anonymous_stock_id",
        "as_of",
        "input_packet_sha256",
        "question_manifest_sha256",
        "evidence_catalog_sha256",
    ):
        if semantic.get(field) != packet.get(field):
            errors.append(f"{field} differs from packet")

    manifest = _manifest(packet)
    required_manifest_fields = {
        "anchor_candidates", "relation_candidates", "stop_candidates", "global_question_ids",
    }
    if not isinstance(packet.get("question_manifest"), dict):
        errors.append("packet: question_manifest must be an object")
    else:
        missing_manifest = required_manifest_fields - set(manifest)
        extra_manifest = set(manifest) - required_manifest_fields
        if missing_manifest:
            errors.append(
                "packet: question_manifest missing fields: " + ",".join(sorted(missing_manifest))
            )
        if extra_manifest:
            errors.append(
                "packet: question_manifest unexpected fields: " + ",".join(sorted(extra_manifest))
            )
    manifest_subjects: dict[str, dict[str, set[str]]] = {}
    for output_group, manifest_field, question_group in (
        ("anchor_candidates", "anchor_candidates", "anchor"),
        ("relation_candidates", "relation_candidates", "relation"),
        ("stop_candidates", "stop_candidates", "stop"),
    ):
        entries = manifest.get(manifest_field)
        if not isinstance(entries, list):
            errors.append(f"packet: question_manifest {manifest_field} must be an array")
            entries = []
        required_by_subject: dict[str, set[str]] = {}
        allowed_question_ids = set(schema["x-question-groups"][question_group])
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"subject_ref", "required_question_ids"}:
                errors.append(f"packet: {manifest_field} entries require exactly subject_ref and required_question_ids")
                continue
            subject_ref = str(entry.get("subject_ref") or "")
            question_ids = entry.get("required_question_ids")
            if not isinstance(question_ids, list) or not question_ids:
                errors.append(f"packet: {manifest_field} {subject_ref} required_question_ids must be nonempty")
                question_ids = []
            normalized_ids = [str(question_id) for question_id in question_ids]
            if len(normalized_ids) != len(set(normalized_ids)):
                errors.append(f"packet: {manifest_field} {subject_ref} has duplicate question ids")
            if not set(normalized_ids).issubset(allowed_question_ids):
                errors.append(f"packet: {manifest_field} {subject_ref} has question id outside its group")
            if not subject_ref or subject_ref in required_by_subject:
                errors.append(f"packet: {manifest_field} subject refs must be nonempty and unique")
            required_by_subject[subject_ref] = set(normalized_ids)
        manifest_subjects[output_group] = required_by_subject

        answer_rows = _candidate_rows(semantic, output_group)
        actual_subjects = [str(row.get("subject_ref") or "") for row in answer_rows]
        if len(actual_subjects) != len(set(actual_subjects)) or set(actual_subjects) != set(required_by_subject):
            errors.append(f"{output_group} must exactly cover manifest subjects once")
        for row in answer_rows:
            subject_ref = str(row.get("subject_ref") or "")
            actual_question_ids = set((row.get("answers") or {}).keys())
            if actual_question_ids != required_by_subject.get(subject_ref, set()):
                errors.append(f"{output_group} {subject_ref} answers must exactly cover required_question_ids")

    global_manifest = manifest.get("global_question_ids")
    if not isinstance(global_manifest, list):
        errors.append("packet: question_manifest global_question_ids must be an array")
        global_manifest = []
    normalized_globals = [str(question_id) for question_id in global_manifest]
    if len(normalized_globals) != len(set(normalized_globals)):
        errors.append("packet: question_manifest global_question_ids contains duplicates")
    if not set(normalized_globals).issubset(set(schema["x-question-groups"]["global"])):
        errors.append("packet: question_manifest global_question_ids contains an unknown question")
    if set((semantic.get("global_answers") or {}).keys()) != set(normalized_globals):
        errors.append("global_answers must exactly cover manifest global_question_ids")

    evidence, evidence_errors = _evidence_map(packet)
    errors.extend(evidence_errors)
    objective = packet.get("objective_facts") or {}
    if not isinstance(packet.get("objective_facts"), dict):
        errors.append("packet: objective_facts must be an object")
        objective = {}
    if "program_verdicts" in objective or "scenario_subjects" in objective or "data_sufficient_for_route" in objective:
        errors.append("packet: objective_facts must be flat and use scenario_hypotheses, not legacy nested fields")
    errors.extend(_validate_route_data_and_comparison_windows(packet, semantic, evidence))
    scenario_hypotheses = objective.get("scenario_hypotheses")
    if not isinstance(scenario_hypotheses, dict) or set(scenario_hypotheses) != set(SCENARIOS):
        errors.append("packet: scenario_hypotheses must contain exactly the four frozen scenarios")
        scenario_hypotheses = {}
    candidate_sets = {
        "anchor_ref": set(manifest_subjects.get("anchor_candidates") or {}),
        "relation_ref": set(manifest_subjects.get("relation_candidates") or {}),
        "episode_stop_ref": set(manifest_subjects.get("stop_candidates") or {}),
        "campaign_stop_ref": set(manifest_subjects.get("stop_candidates") or {}),
    }
    hypothesis_ids: list[str] = []
    required_hypothesis_fields = {
        "hypothesis_id", "anchor_ref", "relation_ref", "episode_stop_ref", "campaign_stop_ref",
        "position_role", "taiji_generation", "same_direction_attack_number", "completed_prior_copy_count",
    }
    for scenario in SCENARIOS:
        hypotheses = scenario_hypotheses.get(scenario)
        if not isinstance(hypotheses, list):
            errors.append(f"packet: {scenario} hypotheses must be an array")
            continue
        for hypothesis in hypotheses:
            if not isinstance(hypothesis, dict):
                errors.append(f"packet: {scenario} hypothesis must be an object")
                continue
            missing = required_hypothesis_fields - set(hypothesis)
            if missing:
                errors.append(f"packet: {scenario} hypothesis missing fields: {','.join(sorted(missing))}")
            hypothesis_id = str(hypothesis.get("hypothesis_id") or "")
            if not hypothesis_id:
                errors.append(f"packet: {scenario} hypothesis_id is required")
            hypothesis_ids.append(hypothesis_id)
            if hypothesis.get("relation_ref") in (None, "", "UNKNOWN", "UNRESOLVED"):
                errors.append(f"packet: {scenario} requires a relation_ref or explicit CURRENT_CONTEXT relation")
            for field, allowed in candidate_sets.items():
                ref = hypothesis.get(field)
                if ref not in (None, "", "UNKNOWN", "UNRESOLVED") and (
                    not isinstance(ref, str) or ref not in allowed
                ):
                    errors.append(f"packet: {scenario} {hypothesis_id} {field} is absent from question manifest")
            if hypothesis.get("position_role") not in (
                None, "UNKNOWN", "UNRESOLVED", "MOTHER", "V2_VALID_ADD",
            ):
                errors.append(f"packet: {scenario} {hypothesis_id} position_role is not a known objective role")
            if hypothesis.get("taiji_generation") not in (
                None, "UNKNOWN", "UNRESOLVED", "ANCHOR_LEG_1", "COPY_LEG_3",
                "COPY_LEG_5", "LATER_GENERATION",
            ):
                errors.append(f"packet: {scenario} {hypothesis_id} taiji_generation is invalid")
            attack_number = hypothesis.get("same_direction_attack_number")
            if attack_number not in (None, "UNKNOWN", "UNRESOLVED") and (
                not isinstance(attack_number, int) or isinstance(attack_number, bool) or attack_number < 1
            ):
                errors.append(f"packet: {scenario} {hypothesis_id} same_direction_attack_number is invalid")
            prior_count = hypothesis.get("completed_prior_copy_count")
            if prior_count not in (None, "UNKNOWN", "UNRESOLVED") and (
                not isinstance(prior_count, int) or isinstance(prior_count, bool) or prior_count < 0
            ):
                errors.append(f"packet: {scenario} {hypothesis_id} completed_prior_copy_count is invalid")
    if len(hypothesis_ids) != len(set(hypothesis_ids)):
        errors.append("packet: hypothesis_id must be globally unique")
    if manifest != expected_question_manifest(packet):
        errors.append("packet: question_manifest differs from deterministic hypothesis/gate requirements")
    signal_event_ref = objective.get("signal_event_ref")
    if signal_event_ref not in (None, "", "UNKNOWN", "UNRESOLVED") and signal_event_ref not in evidence:
        errors.append("packet: signal_event_ref is absent from evidence catalog")
    used_refs = list(_used_evidence_refs(semantic))
    as_of = str(packet.get("as_of") or "")
    for ref, row in evidence.items():
        values = row.get("values") or {}
        visible_dates = [row.get("date")]
        if isinstance(values, dict):
            visible_dates.extend([values.get("confirmation_date"), values.get("available_on")])
        if any(_date_after_as_of(value, as_of) for value in visible_dates if value is not None):
            errors.append(f"future evidence ref: {ref}")
    for ref in used_refs:
        if ref not in evidence:
            errors.append(f"unknown evidence ref: {ref}")
            continue
        row = evidence[ref]

    for verdict in _iter_verdicts(semantic):
        supporting = set(verdict.get("supporting_evidence_refs") or [])
        contradicting = set(verdict.get("contradicting_evidence_refs") or [])
        if supporting.intersection(contradicting):
            errors.append("same evidence ref supports and contradicts one answer")

    attestation = semantic.get("causal_attestation") or {}
    if attestation.get("latest_visible_bar") != packet.get("as_of"):
        errors.append("latest_visible_bar differs from packet as_of")

    forbidden = set(schema.get("x-forbidden-ai-fields") or [])
    for key in _walk_keys(semantic):
        if key in forbidden:
            errors.append(f"AI output contains forbidden derived field: {key}")
    return sorted(set(errors))


def _normalize_verdict(value: Any) -> str:
    if value in VERDICTS:
        return str(value)
    if value is True:
        return "PASS"
    if value is False:
        return "FAIL"
    return "UNKNOWN"


def _and_verdict(values: Iterable[str]) -> str:
    rows = list(values)
    if any(value == "FAIL" for value in rows):
        return "FAIL"
    if not rows or any(value != "PASS" for value in rows):
        return "UNKNOWN"
    return "PASS"


def _answer_indexes(semantic: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    indexes: dict[str, dict[str, dict[str, Any]]] = {}
    for group in ("anchor_candidates", "relation_candidates", "stop_candidates"):
        indexes[group] = {
            str(row["subject_ref"]): row["answers"] for row in _candidate_rows(semantic, group)
        }
    return indexes


def _global_result(semantic: dict[str, Any], question_id: str) -> str:
    return _normalize_verdict(((semantic.get("global_answers") or {}).get(question_id) or {}).get("result"))


def _candidate_result(
    indexes: dict[str, dict[str, dict[str, Any]]],
    group: str,
    subject_ref: Any,
    question_id: str,
) -> str:
    if not subject_ref:
        return "UNKNOWN"
    answer = ((indexes.get(group) or {}).get(str(subject_ref)) or {}).get(question_id) or {}
    return _normalize_verdict(answer.get("result"))


def derive_quadrants(semantic: dict[str, Any]) -> dict[str, str]:
    def quadrant(scale: str) -> str:
        trend = _global_result(semantic, f"{scale}_TREND_STRENGTH_INCREASED")
        volatility = _global_result(semantic, f"{scale}_VOLATILITY_EXPANDED")
        if "UNKNOWN" in {trend, volatility}:
            return "UNRESOLVED"
        return {
            ("PASS", "PASS"): "Q1",
            ("FAIL", "PASS"): "Q2",
            ("FAIL", "FAIL"): "Q3",
            ("PASS", "FAIL"): "Q4",
        }[(trend, volatility)]

    return {"large": quadrant("LARGE"), "small": quadrant("SMALL")}


def derive_scale_relationship(semantic: dict[str, Any]) -> str:
    large = _global_result(semantic, "LARGE_NEXT_UP_DIRECTION_SUPPORTED")
    small = _global_result(semantic, "SMALL_NEXT_UP_DIRECTION_SUPPORTED")
    if "UNKNOWN" in {large, small}:
        return "UNRESOLVED"
    if large == small == "PASS":
        return "DIRECTION_RESONANCE"
    if large != small:
        return "CONFLICT"
    return "NO_LONG_ALIGNMENT"


def derive_stage(semantic: dict[str, Any], hypothesis: dict[str, Any]) -> str:
    indexes = _answer_indexes(semantic)
    relation_ref = hypothesis.get("relation_ref")

    def relation_result(question_id: str) -> str:
        return _candidate_result(indexes, "relation_candidates", relation_ref, question_id)

    generation = hypothesis.get("taiji_generation")
    attack_number = hypothesis.get("same_direction_attack_number")
    weak_ids = (
        "EXH_ATTACK_SHORTENING",
        "EXH_SLOPE_DECAY",
        "EXH_PRICE_VOLUME_DIVERGENCE",
        "EXH_FAILED_CONTINUATION",
    )
    weak = [relation_result(question_id) for question_id in weak_ids]
    time_space = relation_result("EXH_TIME_SPACE_EXHAUSTION")
    remaining = relation_result("LOCATION_REMAINING_SPACE_ADEQUATE")
    extended = relation_result("LOCATION_NOT_EXTENDED_FROM_ORIGIN")
    weak_pass = weak.count("PASS")
    late_generation = generation in {"COPY_LEG_5", "LATER_GENERATION"}

    if time_space == "PASS" or remaining == "FAIL" or (late_generation and weak_pass >= 2):
        return "EXHAUSTED"
    if late_generation or (isinstance(attack_number, int) and attack_number >= 3 and weak_pass >= 1):
        return "LATE"
    if generation in {"ANCHOR_LEG_1", "COPY_LEG_3"} and extended == "PASS" and all(
        value == "FAIL" for value in weak
    ) and time_space == "FAIL" and remaining == "PASS" and (
        attack_number is None or (isinstance(attack_number, int) and attack_number <= 2)
    ):
        return "EARLY"
    if generation in {"ANCHOR_LEG_1", "COPY_LEG_3"} and all(value != "UNKNOWN" for value in weak) and (
        time_space != "UNKNOWN" and remaining != "UNKNOWN"
    ):
        return "MIDDLE"
    return "UNRESOLVED"


def _fact(objective: dict[str, Any], hypothesis: dict[str, Any], name: str) -> Any:
    return hypothesis[name] if name in hypothesis else objective.get(name)


def derive_left_right_phase(objective: dict[str, Any], hypothesis: dict[str, Any] | None = None) -> str:
    hypothesis = hypothesis or {}
    large_dow = _fact(objective, hypothesis, "large_dow_state")
    context = _fact(objective, hypothesis, "bear_reversal_context")
    if context is False:
        return "NONE"
    if context is None and large_dow != "BEAR":
        return "NONE" if large_dow in {"BULL", "TRANSITION"} else "UNRESOLVED"
    direct = _fact(objective, hypothesis, "direct_same_clean_impulse")
    large_broken = _fact(objective, hypothesis, "large_bear_defense_broken")
    small_bull = _fact(objective, hypothesis, "small_bull_control")
    if direct is True:
        return "DIRECT_TO_RIGHT"
    if large_broken is False:
        if small_bull is True:
            return "LR"
        if small_bull is False:
            return "LL"
        return "UNRESOLVED"
    if large_broken is True:
        if _fact(objective, hypothesis, "rr_break_completed") is True:
            return "RR"
        if _fact(objective, hypothesis, "first_retest_after_large_break_held") is True:
            return "RL"
    return "UNRESOLVED"


def _program_result(
    source_name: str,
    objective: dict[str, Any],
    derived: dict[str, Any],
    scenario: str,
    hypothesis: dict[str, Any],
) -> str:
    direct_names = {
        "ACTIVE_WATCHLIST": "active_watchlist",
        "TRIGGER_COMPLETED": "trigger_completed",
        "RISK_EXECUTABLE": "risk_executable",
        "LARGE_BULL_DEFENSE_INTACT": "large_bull_defense_intact",
        "CORRECTION_BEAR_DOW_LINE_CAUSAL": "correction_bear_dow_line_causal",
        "SMALL_UP_CONTROL_BREAK": "small_up_control_break",
        "STOP_CAUSAL_FIELDS_VALID": "stop_causal_fields_valid",
        "LARGE_BEAR_DOW_DEFENSE_CAUSAL": "large_bear_dow_defense_causal",
        "PHASE_STOP_CAUSAL": "phase_stop_causal",
        "MACD_IS_SUPPORT_ONLY": "macd_is_support_only",
        "FRESH_ANCHOR_STOP_CAUSAL": "fresh_anchor_stop_causal",
    }
    if source_name in direct_names:
        value = _fact(objective, hypothesis, direct_names[source_name])
        return _normalize_verdict(value)
    if source_name == "DATA_SUFFICIENT_FOR_ROUTE":
        row = (objective.get("data_sufficiency_by_route") or {}).get(scenario) or {}
        return _normalize_verdict(row.get("status"))
    if source_name == "TAIJI_GENERATION":
        return "PASS" if hypothesis.get("taiji_generation") in {
            "ANCHOR_LEG_1", "COPY_LEG_3", "COPY_LEG_5", "LATER_GENERATION"
        } else "UNKNOWN"
    if source_name == "SCALE_RELATIONSHIP":
        return "UNKNOWN" if derived["scale_relationship"] == "UNRESOLVED" else (
            "FAIL" if derived["scale_relationship"] == "CONFLICT" else "PASS"
        )
    if source_name == "STAGE_AND_EXHAUSTION":
        stage = derived.get("stage", "UNRESOLVED")
        return "UNKNOWN" if stage == "UNRESOLVED" else (
            "FAIL" if stage == "EXHAUSTED" else "PASS"
        )
    if source_name == "LARGE_DOW_BEAR":
        state = _fact(objective, hypothesis, "large_dow_state")
        return "PASS" if state == "BEAR" else ("UNKNOWN" if state in {None, "UNDEFINED"} else "FAIL")
    if source_name == "LEFT_RIGHT_PHASE":
        return "PASS" if derived["left_right_phase"] in {"LL", "LR", "RL", "RR", "DIRECT_TO_RIGHT"} else (
            "UNKNOWN" if derived["left_right_phase"] == "UNRESOLVED" else "FAIL"
        )
    if source_name == "LARGE_SMALL_DOW_SEPARATED":
        large_state = _fact(objective, hypothesis, "large_dow_state")
        small_state = _fact(objective, hypothesis, "small_dow_state")
        if large_state in {None, "UNDEFINED", "UNKNOWN", "UNRESOLVED"} or small_state in {
            None, "UNDEFINED", "UNKNOWN", "UNRESOLVED"
        }:
            return "UNKNOWN"
        return "PASS" if large_state != small_state else "FAIL"
    return "UNKNOWN"


def _source_result(
    source: str,
    scenario: str,
    semantic: dict[str, Any],
    indexes: dict[str, dict[str, dict[str, Any]]],
    subjects: dict[str, Any],
    objective: dict[str, Any],
    derived: dict[str, Any],
    hypothesis: dict[str, Any],
) -> str:
    if source.startswith("PROGRAM:"):
        return _program_result(source.split(":", 1)[1], objective, derived, scenario, hypothesis)
    schema = read_json(SCHEMA_PATH)
    groups = schema["x-question-groups"]
    if source in groups["global"]:
        return _global_result(semantic, source)
    if source in groups["anchor"]:
        return _candidate_result(indexes, "anchor_candidates", subjects.get("anchor_ref"), source)
    if source in groups["relation"]:
        return _candidate_result(indexes, "relation_candidates", subjects.get("relation_ref"), source)
    if source in groups["stop"]:
        stop_ref = subjects.get("episode_stop_ref") if source == "STOP_BELONGS_TO_CURRENT_EPISODE" else subjects.get("campaign_stop_ref")
        return _candidate_result(indexes, "stop_candidates", stop_ref, source)
    return "UNKNOWN"


def _not_q3(quadrants: dict[str, str]) -> str:
    values = set(quadrants.values())
    if "Q3" in values:
        return "FAIL"
    if "UNRESOLVED" in values:
        return "UNKNOWN"
    return "PASS"


def build_gate_matrix(
    semantic: dict[str, Any], objective: dict[str, Any], base_derived: dict[str, Any] | None = None
) -> dict[str, dict[str, dict[str, Any]]]:
    schema = read_json(SCHEMA_PATH)
    metadata = schema["x-four-scenario-gate-matrix"]
    indexes = _answer_indexes(semantic)
    scenario_hypotheses = objective.get("scenario_hypotheses") or {}
    base_derived = base_derived or {
        "quadrants": derive_quadrants(semantic),
        "scale_relationship": derive_scale_relationship(semantic),
    }
    output: dict[str, dict[str, dict[str, Any]]] = {scenario: {} for scenario in SCENARIOS}
    for scenario, gates in metadata.items():
        for hypothesis in scenario_hypotheses.get(scenario) or []:
            hypothesis_id = str(hypothesis.get("hypothesis_id") or "")
            derived = {
                **base_derived,
                "stage": derive_stage(semantic, hypothesis),
                "left_right_phase": derive_left_right_phase(objective, hypothesis),
            }
            hypothesis_gates: dict[str, dict[str, Any]] = {}
            for gate_id, sources in gates.items():
                source_results = {
                    source: _source_result(
                        source, scenario, semantic, indexes, hypothesis, objective, derived, hypothesis
                    )
                    for source in sources
                }
                result = _and_verdict(source_results.values())
                if gate_id == "DYNAMIC_QUADRANTS_SUPPORT":
                    result = _and_verdict([
                        _not_q3(derived["quadrants"]),
                        "FAIL" if derived["scale_relationship"] == "CONFLICT" else (
                            "UNKNOWN" if derived["scale_relationship"] == "UNRESOLVED" else "PASS"
                        ),
                    ])
                elif gate_id == "DYNAMIC_Q1_EXPANSION":
                    quadrants = derived["quadrants"]
                    result = "PASS" if quadrants == {"large": "Q1", "small": "Q1"} else (
                        "FAIL" if "Q3" in quadrants.values() or any(value in {"Q2", "Q4"} for value in quadrants.values())
                        else "UNKNOWN"
                    )
                elif gate_id == "NOT_Q3_OR_EXHAUSTED":
                    result = _and_verdict([
                        _not_q3(derived["quadrants"]),
                        "FAIL" if derived["stage"] == "EXHAUSTED" else (
                            "UNKNOWN" if derived["stage"] == "UNRESOLVED" else "PASS"
                        ),
                    ])
                elif gate_id == "LEFT_RIGHT_PHASE_MAPPED":
                    result = _program_result("LEFT_RIGHT_PHASE", objective, derived, scenario, hypothesis)
                elif gate_id == "DUAL_SCALE_SEPARATED":
                    result = _program_result("LARGE_SMALL_DOW_SEPARATED", objective, derived, scenario, hypothesis)
                elif gate_id == "BEAR_LATE_STAGE_EVIDENCE":
                    relation_ref = hypothesis.get("relation_ref")
                    late = _candidate_result(indexes, "relation_candidates", relation_ref, "BEAR_ATTACK_IS_LATE_STAGE")
                    partial = _candidate_result(indexes, "relation_candidates", relation_ref, "BEAR_LATE_STAGE_PARTIAL")
                    clues = [
                        _candidate_result(indexes, "relation_candidates", relation_ref, name) for name in (
                            "EXH_ATTACK_SHORTENING", "EXH_SLOPE_DECAY", "EXH_PRICE_VOLUME_DIVERGENCE",
                            "EXH_FAILED_CONTINUATION", "EXH_TIME_SPACE_EXHAUSTION",
                        )
                    ]
                    result = "PASS" if late == "PASS" and "PASS" in clues else (
                        "FAIL" if late == "FAIL" else ("UNKNOWN" if partial == "PASS" else "UNKNOWN")
                    )
                elif gate_id == "EARLY_TAIJI_GENERATION":
                    result = _and_verdict([
                        result,
                        "PASS" if derived["stage"] == "EARLY" else (
                            "UNKNOWN" if derived["stage"] == "UNRESOLVED" else "FAIL"
                        ),
                    ])
                hypothesis_gates[gate_id] = {"result": result, "source_results": source_results}
            output[scenario][hypothesis_id] = {
                "hypothesis": hypothesis,
                "derived": derived,
                "gates": hypothesis_gates,
                "scenario_boundary": _scenario_boundary(scenario, semantic, hypothesis, hypothesis_gates, objective),
            }
    return output


def _scenario_boundary(
    scenario: str,
    semantic: dict[str, Any],
    hypothesis: dict[str, Any],
    gates: dict[str, dict[str, Any]],
    objective: dict[str, Any],
) -> str:
    indexes = _answer_indexes(semantic)
    relation_ref = hypothesis.get("relation_ref")
    if scenario == "BEAR_REVERSAL_LEFT_RIGHT":
        return _normalize_verdict(_fact(objective, hypothesis, "bear_reversal_context"))
    if scenario == "MACRO_COPY_RESONANCE":
        replication = _candidate_result(indexes, "relation_candidates", relation_ref, "REL_CURRENT_LEG_IS_REPLICATION")
        generation = hypothesis.get("taiji_generation")
        prior = hypothesis.get("completed_prior_copy_count")
        if replication == "FAIL" or generation in {"ANCHOR_LEG_1", "COPY_LEG_5", "LATER_GENERATION"} or (
            isinstance(prior, int) and prior != 0
        ):
            return "FAIL"
        if replication == "PASS" and generation == "COPY_LEG_3" and prior == 0:
            return "PASS"
        return "UNKNOWN"
    if scenario == "MATURE_TREND_PULLBACK":
        repeated = _candidate_result(indexes, "relation_candidates", relation_ref, "LONG_CAMPAIGN_REPEATED_SUCCESS")
        prior = hypothesis.get("completed_prior_copy_count")
        if repeated == "FAIL" or (isinstance(prior, int) and prior < 1):
            return "FAIL"
        if repeated == "PASS" and isinstance(prior, int) and prior >= 1:
            return "PASS"
        return "UNKNOWN"
    fresh = _candidate_result(indexes, "relation_candidates", relation_ref, "REL_CURRENT_LEG_IS_FRESH_ANCHOR")
    return fresh


def derive_primary_scenario(matrix: dict[str, dict[str, dict[str, Any]]]) -> str:
    """Apply the frozen semantic boundary order: bear -> first macro copy -> mature -> fresh."""

    ordered = (
        "BEAR_REVERSAL_LEFT_RIGHT",
        "MACRO_COPY_RESONANCE",
        "MATURE_TREND_PULLBACK",
        "FRESH_Q1_EXPANSION",
    )
    for scenario in ordered:
        rows = list(matrix.get(scenario, {}).values())
        boundaries = [row["scenario_boundary"] for row in rows]
        if "PASS" in boundaries:
            return scenario
        if "UNKNOWN" in boundaries:
            return "UNRESOLVED_NO_TRADE"
    return "NO_TRADE"


def _common_hard_guards(
    scenario: str,
    semantic: dict[str, Any],
    objective: dict[str, Any],
    evaluation: dict[str, Any],
) -> dict[str, str]:
    scenario_gates = evaluation["gates"]
    derived = evaluation["derived"]
    hypothesis = evaluation["hypothesis"]
    indexes = _answer_indexes(semantic)
    stop_gate = {
        "MATURE_TREND_PULLBACK": "EPISODE_STOP_CAUSAL",
        "MACRO_COPY_RESONANCE": "EPISODE_STOP_CAUSAL",
        "BEAR_REVERSAL_LEFT_RIGHT": "PHASE_STOP_CAUSAL",
        "FRESH_Q1_EXPANSION": "FRESH_ANCHOR_STOP_CAUSAL",
    }[scenario]
    no_v2_fail = "FAIL" if any(row["result"] == "FAIL" for row in scenario_gates.values()) else "PASS"
    relationship = derived["scale_relationship"]
    stage = derived["stage"]
    if scenario == "BEAR_REVERSAL_LEFT_RIGHT":
        separated = scenario_gates["DUAL_SCALE_SEPARATED"]["result"]
        small_up = _global_result(semantic, "SMALL_NEXT_UP_DIRECTION_SUPPORTED")
        large_dow = _fact(objective, hypothesis, "large_dow_state")
        no_scale_conflict = _and_verdict([
            separated,
            small_up,
            "PASS" if large_dow == "BEAR" else ("UNKNOWN" if large_dow in {None, "UNDEFINED"} else "FAIL"),
        ])
    else:
        no_scale_conflict = "PASS" if relationship == "DIRECTION_RESONANCE" else (
            "UNKNOWN" if relationship == "UNRESOLVED" else "FAIL"
        )
    return {
        "ACTIVE_WATCHLIST": _program_result("ACTIVE_WATCHLIST", objective, derived, scenario, hypothesis),
        "DATA_SUFFICIENT_FOR_ROUTE": _program_result("DATA_SUFFICIENT_FOR_ROUTE", objective, derived, scenario, hypothesis),
        "TRIGGER_COMPLETED": _program_result("TRIGGER_COMPLETED", objective, derived, scenario, hypothesis),
        "CAUSAL_EPISODE_STOP": scenario_gates[stop_gate]["result"],
        "CAMPAIGN_STOP_CAUSAL": _candidate_result(
            indexes,
            "stop_candidates",
            hypothesis.get("campaign_stop_ref"),
            "STOP_BELONGS_TO_PARENT_CAMPAIGN",
        ),
        "NOT_Q3": _not_q3(derived["quadrants"]),
        "NOT_LATE_OR_EXHAUSTED": "PASS" if stage in {"EARLY", "MIDDLE"} else (
            "FAIL" if stage in {"LATE", "EXHAUSTED"} else "UNKNOWN"
        ),
        "NO_SCALE_DIRECTION_CONFLICT": no_scale_conflict,
        "NOT_SINGLE_INDICATOR_SIGNAL": _global_result(semantic, "SIGNAL_HAS_INDEPENDENT_STRUCTURE"),
        "RISK_EXECUTABLE": _program_result("RISK_EXECUTABLE", objective, derived, scenario, hypothesis),
        "NO_V2_FAIL": no_v2_fail,
    }


def _all_pass(rows: dict[str, str] | dict[str, dict[str, Any]], names: Iterable[str] | None = None) -> bool:
    selected = rows.values() if names is None else (rows[name] for name in names)
    return all((row if isinstance(row, str) else row["result"]) == "PASS" for row in selected)


def _fresh_residual_ok(
    unknown: str,
    semantic: dict[str, Any],
    hypothesis: dict[str, Any],
    derived: dict[str, Any],
) -> bool:
    indexes = _answer_indexes(semantic)
    relation_ref = hypothesis.get("relation_ref")
    if unknown == "CLEAN_MEATY_DESTRUCTIVE_TRACEABLE":
        anchor_ref = hypothesis.get("anchor_ref")
        return all(_candidate_result(indexes, "anchor_candidates", anchor_ref, name) == "PASS" for name in (
            "ANCHOR_CLEAN", "ANCHOR_MEATY", "ANCHOR_TRACEABLE"
        )) and _candidate_result(indexes, "anchor_candidates", anchor_ref, "ANCHOR_DESTRUCTIVE") == "UNKNOWN"
    if unknown == "DYNAMIC_Q1_EXPANSION":
        return derived["quadrants"]["small"] == "Q1" and derived["quadrants"]["large"] == "UNRESOLVED"
    if unknown == "EARLY_TAIJI_GENERATION":
        attack = hypothesis.get("same_direction_attack_number")
        return derived["stage"] == "EARLY" and (attack is None or attack <= 2) and hypothesis.get("taiji_generation") not in {
            "COPY_LEG_5", "LATER_GENERATION"
        }
    return False


def _eligible_route(
    scenario: str,
    semantic: dict[str, Any],
    objective: dict[str, Any],
    evaluation: dict[str, Any],
    guards: dict[str, str],
) -> tuple[str | None, str | None]:
    gates = evaluation["gates"]
    derived = evaluation["derived"]
    hypothesis = evaluation["hypothesis"]
    if evaluation["scenario_boundary"] != "PASS":
        return None, None
    if not _all_pass(guards):
        return None, None
    if _all_pass(gates):
        return "V2_CORE", None
    route = SCENARIO_ROUTE.get(scenario)
    if not route:
        return None, None
    rules = read_json(V3_RULES_PATH)["layers"][route]
    hard = rules["hard_pass_gates"]
    soft = rules["soft_gates"]
    if not _all_pass(gates, hard):
        return None, None
    soft_results = {name: gates[name]["result"] for name in soft}
    if "FAIL" in soft_results.values():
        return None, None
    unknowns = [name for name, result in soft_results.items() if result == "UNKNOWN"]
    if len(unknowns) != 1 or any(result != "PASS" for name, result in soft_results.items() if name != unknowns[0]):
        return None, None
    unknown = unknowns[0]
    if scenario == "FRESH_Q1_EXPANSION" and not _fresh_residual_ok(unknown, semantic, hypothesis, derived):
        return None, None
    if scenario == "BEAR_REVERSAL_LEFT_RIGHT":
        if unknown != "BEAR_LATE_STAGE_EVIDENCE" or derived["left_right_phase"] not in {"LR", "RL", "RR", "DIRECT_TO_RIGHT"}:
            return None, None
        relation_ref = hypothesis.get("relation_ref")
        if _candidate_result(_answer_indexes(semantic), "relation_candidates", relation_ref, "BEAR_LATE_STAGE_PARTIAL") != "PASS":
            return None, None
    return route, unknown


def _signature(
    route: str, hypothesis: dict[str, Any], objective: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[str]]:
    position_role = "PROBE_MOTHER" if route != "V2_CORE" else hypothesis.get("position_role")
    signature = {
        "direction": "UP",
        "signal_event_ref": objective.get("signal_event_ref"),
        "episode_stop_ref": hypothesis.get("episode_stop_ref"),
        "campaign_stop_ref": hypothesis.get("campaign_stop_ref"),
        "position_role": position_role,
    }
    missing = [
        name for name, value in signature.items()
        if value in (None, "", "UNKNOWN", "UNRESOLVED")
    ]
    return (None, missing) if missing else (signature, [])


def reduce_atomic_v3(packet: dict[str, Any], semantic: dict[str, Any]) -> dict[str, Any]:
    """Validate and deterministically reduce one anonymous stock-day to policy."""

    errors = validate_atomic(packet, semantic)
    if errors:
        invalid_code = "INVALID_PACKET" if any(error.startswith("packet:") for error in errors) else "INVALID_AI_OUTPUT"
        return {
            "permission": "WAIT",
            "route": "NO_TRADE",
            "reason_codes": [invalid_code],
            "validation_errors": errors,
        }

    objective = packet.get("objective_facts") or {}
    base_derived = {
        "quadrants": derive_quadrants(semantic),
        "scale_relationship": derive_scale_relationship(semantic),
    }
    matrix = build_gate_matrix(semantic, objective, base_derived)
    primary_scenario = derive_primary_scenario(matrix)
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
    if primary_scenario in SCENARIOS:
        primary_states = [row["derived"] for row in matrix[primary_scenario].values()]
        for field in ("stage", "left_right_phase"):
            values = {state[field] for state in primary_states}
            derived[field] = next(iter(values)) if len(values) == 1 else "UNRESOLVED"

    invalidated = _normalize_verdict(objective.get("parent_campaign_invalidated"))
    alert = _normalize_verdict(objective.get("macro_defense_alert"))
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
        scenario: {} for scenario in SCENARIOS
    }
    hypotheses: list[dict[str, Any]] = []
    incomplete_signatures: list[dict[str, Any]] = []
    for scenario in SCENARIOS:
        for hypothesis_id, evaluation in matrix.get(scenario, {}).items():
            guards = _common_hard_guards(scenario, semantic, objective, evaluation)
            guards_by_scenario[scenario][hypothesis_id] = guards
            route, unknown = _eligible_route(scenario, semantic, objective, evaluation, guards)
            if not route:
                continue
            signature, missing = _signature(route, evaluation["hypothesis"], objective)
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
        json.dumps(row["action_signature"], ensure_ascii=False, sort_keys=True) for row in hypotheses
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

    # An eligible alternative with unresolved execution semantics is material even
    # when another eligible hypothesis is complete; do not silently discard it.
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
                    "PRIMARY_SCENARIO_UNRESOLVED" if primary == "UNRESOLVED_NO_TRADE"
                    else "PRIMARY_SCENARIO_CONFLICT"
                ],
                "derived_structure": derived,
                "gate_matrix": matrix,
                "common_hard_guards": guards_by_scenario,
                "hypotheses": hypotheses,
            }
        matching = [row for row in hypotheses if row["scenario"] == primary]
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
            key=lambda row: (ROUTE_PRECEDENCE[row["route"]], row["hypothesis_id"]),
        )
        selected_state = matrix[selected["scenario"]][selected["hypothesis_id"]]["derived"]
        derived["stage"] = selected_state["stage"]
        derived["left_right_phase"] = selected_state["left_right_phase"]
        derived["selected_hypothesis_id"] = selected["hypothesis_id"]
        return {
            "permission": "TRADE",
            "route": selected["route"],
            "scenario": selected["scenario"],
            "unknown_gate": selected["unknown_gate"],
            "action_signature": selected["action_signature"],
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


def validate_course_invariants(
    packet: dict[str, Any], semantic: dict[str, Any], decision: dict[str, Any]
) -> list[str]:
    """Check deterministic hard policy invariants, not human semantic gold correctness."""

    errors = [f"atomic contract invalid: {error}" for error in validate_atomic(packet, semantic)]
    if errors:
        return errors

    expected = reduce_atomic_v3(packet, semantic)
    material_fields = (
        "permission", "route", "scenario", "unknown_gate", "action_signature", "reason_codes",
    )
    for field in material_fields:
        if decision.get(field) != expected.get(field):
            errors.append(f"decision material field differs from deterministic reducer: {field}")

    expected_derived = expected.get("derived_structure") or {}
    actual_derived = decision.get("derived_structure") or {}
    for field in ("primary_scenario", "selected_hypothesis_id", "stage", "left_right_phase"):
        if actual_derived.get(field) != expected_derived.get(field):
            errors.append(f"decision derived field differs from deterministic reducer: {field}")

    permission = decision.get("permission")
    route = decision.get("route")
    scenario = decision.get("scenario")
    unresolved = {None, "", "UNKNOWN", "UNRESOLVED", "UNRESOLVED_NO_TRADE", "NO_TRADE"}
    if permission == "TRADE":
        if route in unresolved:
            errors.append("TRADE requires a resolved non-NO_TRADE route")
        if scenario in unresolved:
            errors.append("TRADE requires a resolved scenario")
        if actual_derived.get("left_right_phase") in unresolved:
            errors.append("TRADE requires a resolved phase")
        signature = decision.get("action_signature")
        required_signature = {
            "direction", "signal_event_ref", "episode_stop_ref", "campaign_stop_ref", "position_role",
        }
        if not isinstance(signature, dict) or set(signature) != required_signature:
            errors.append("TRADE requires the exact complete action signature")
            signature = {}
        for field in required_signature:
            if signature.get(field) in unresolved:
                errors.append(f"TRADE action signature unresolved: {field}")

        selected_id = actual_derived.get("selected_hypothesis_id")
        selected_hypothesis = next(
            (
                hypothesis
                for hypothesis in ((packet.get("objective_facts") or {}).get("scenario_hypotheses") or {}).get(scenario, [])
                if hypothesis.get("hypothesis_id") == selected_id
            ),
            {},
        )
        campaign_result = _candidate_result(
            _answer_indexes(semantic),
            "stop_candidates",
            selected_hypothesis.get("campaign_stop_ref"),
            "STOP_BELONGS_TO_PARENT_CAMPAIGN",
        )
        if campaign_result != "PASS":
            errors.append("TRADE campaign stop lacks causal parent-campaign PASS evidence")
        guards = (((decision.get("common_hard_guards") or {}).get(scenario) or {}).get(selected_id) or {})
        if not guards or any(value != "PASS" for value in guards.values()):
            errors.append("TRADE has a failed or unresolved common hard guard")

        gate_row = (((decision.get("gate_matrix") or {}).get(scenario) or {}).get(selected_id) or {})
        gates = gate_row.get("gates") or {}
        if route == "V2_CORE":
            if not gates or any(row.get("result") != "PASS" for row in gates.values()):
                errors.append("V2_CORE TRADE requires all selected scenario gates PASS")
        elif route in {"NEAR_PASS_MACRO_COPY", "NEAR_PASS_FRESH_Q1", "BEAR_REVERSAL_PROBE"}:
            rules = read_json(V3_RULES_PATH)["layers"][route]
            hard_results = [((gates.get(name) or {}).get("result")) for name in rules["hard_pass_gates"]]
            soft_results = [((gates.get(name) or {}).get("result")) for name in rules["soft_gates"]]
            if any(value != "PASS" for value in hard_results):
                errors.append(f"{route} TRADE requires every route hard gate PASS")
            if soft_results.count("UNKNOWN") != 1 or any(
                value not in {"PASS", "UNKNOWN"} for value in soft_results
            ):
                errors.append(f"{route} TRADE violates the single allowed UNKNOWN contract")

    if permission == "REMOVE" and (packet.get("objective_facts") or {}).get("parent_campaign_invalidated") is not True:
        errors.append("REMOVE requires objective parent_campaign_invalidated is True")
    if "MATERIAL_HYPOTHESIS_CONFLICT" in (decision.get("reason_codes") or []) and permission != "WAIT":
        errors.append("MATERIAL_HYPOTHESIS_CONFLICT must resolve to WAIT")
    if route == "BEAR_REVERSAL_PROBE" and actual_derived.get("left_right_phase") == "LL":
        errors.append("BEAR_REVERSAL_PROBE is forbidden in LL phase")
    return sorted(set(errors))
