"""Final deterministic holdout and three-run consistency tooling for protocol V2.

The module has two deliberately separate layers:

* pure functions build an outcome-blind, immutable sample plan and evaluate
  already-produced atomic answers.  Unit tests and pre-freeze development use
  these functions without requiring the rest of the protocol to be FINAL;
* the CLI is a formal entry point and fails closed unless the packet builder
  and atomic policy declare their exact production versions and ``FINAL``
  status.

No price outcome, identity map, prior result, or backtest performance is read.
Disagreement is never resolved by majority vote: every disagreeing atomic
verdict is replaced by ``UNKNOWN`` before the policy reducer is called again.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import importlib.util
import json
import math
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

try:
    from .hybrid_v3_atomic_packets_v2 import assert_anonymous_and_causal
except ImportError:  # direct script/importlib execution from the repository root
    from scripts.hybrid_v3_atomic_packets_v2 import assert_anonymous_and_causal


CONSISTENCY_VERSION = "hybrid-v3-consistency-v2"
CONSISTENCY_STATUS = "FINAL"
HOLDOUT_PLAN_VERSION = "hybrid-v3-holdout-plan-v2-draft"
DEFAULT_RESERVE_BLOCKS = 2
VERDICTS = {"PASS", "FAIL", "UNKNOWN"}

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTOCOL = ROOT / "config/hybrid_monitoring_protocol_v2.json"
DEFAULT_SCHEMA = ROOT / "config/hybrid_atomic_semantics_v2.schema.json"
DEFAULT_POLICY = ROOT / "scripts/hybrid_v3_atomic_policy_v2.py"
DEFAULT_PACKET_BUILDER = ROOT / "scripts/hybrid_v3_atomic_packets_v2.py"

ValidateFn = Callable[[dict[str, Any], dict[str, Any]], Sequence[str]]
ReduceFn = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
CourseInvariantFn = Callable[
    [dict[str, Any], dict[str, Any], dict[str, Any]], Sequence[str]
]


class ConsistencyError(ValueError):
    """The holdout or repeated-run evidence is incomplete or mutable."""


class FormalReadinessError(ConsistencyError):
    """A formal CLI dependency is missing, DRAFT, or has the wrong version."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_sha256(value: Any, label: str) -> str:
    result = str(value or "").lower()
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise ConsistencyError(f"{label} must be a lowercase SHA-256")
    return result


def _record_field(record: Mapping[str, Any], field: str) -> Any:
    if field in record:
        return record[field]
    packet = record.get("packet")
    return packet.get(field) if isinstance(packet, Mapping) else None


def _normalized_case(record: Mapping[str, Any]) -> dict[str, Any]:
    packet = record.get("packet")
    review_id = str(_record_field(record, "review_id") or "")
    stock_id = str(_record_field(record, "anonymous_stock_id") or "")
    stratum = str(_record_field(record, "sampling_stratum") or "")
    if not review_id or not stock_id or not stratum:
        raise ConsistencyError("each source row needs review_id, anonymous_stock_id, and sampling_stratum")
    try:
        ordinal = int(_record_field(record, "source_ordinal"))
    except (TypeError, ValueError) as exc:
        raise ConsistencyError(f"invalid source_ordinal for {review_id}") from exc
    if ordinal < 0:
        raise ConsistencyError(f"negative source_ordinal for {review_id}")
    if isinstance(packet, Mapping):
        calculated = canonical_sha256(packet)
        supplied = record.get("packet_sha256")
        if supplied is not None and _require_sha256(supplied, "packet_sha256") != calculated:
            raise ConsistencyError(f"packet hash mismatch for {review_id}")
        packet_sha = calculated
    else:
        packet_sha = _require_sha256(_record_field(record, "packet_sha256"), "packet_sha256")
    return {
        "source_ordinal": ordinal,
        "review_id": review_id,
        "anonymous_stock_id": stock_id,
        "sampling_stratum": stratum,
        "packet_sha256": packet_sha,
    }


def _assert_outcome_blind_source(
    records: Sequence[Mapping[str, Any]], *, outcome_blind_locked: bool
) -> None:
    if outcome_blind_locked is not True:
        raise ConsistencyError("sampling source is not attested LOCKED_OUTCOME_BLIND")
    for source in records:
        packet = source.get("packet")
        if not isinstance(packet, Mapping):
            raise ConsistencyError("outcome-blind source row must contain the official atomic packet")
        as_of = str(packet.get("as_of") or "")
        try:
            # This is the packet builder's canonical recursive guard.  Checking
            # only visibility flags is insufficient because a nested MFE/future
            # field would also change packet_sha256 and therefore sample rank.
            assert_anonymous_and_causal(dict(packet), as_of=as_of)
        except Exception as exc:
            raise ConsistencyError(f"outcome-blind source packet is not causal: {exc}") from exc


def _verified_report_hash(report: Mapping[str, Any], field: str) -> bool:
    supplied = str(report.get(field) or "").lower()
    if len(supplied) != 64 or any(char not in "0123456789abcdef" for char in supplied):
        return False
    core = dict(report)
    core.pop(field, None)
    return supplied == canonical_sha256(core)


def _research_calibration_gate(
    report: Mapping[str, Any] | None, protocol: Mapping[str, Any]
) -> dict[str, Any]:
    contract = ((protocol.get("correctness_gate") or {}).get("research_calibration") or {})
    required_balance = contract.get("required_balance") or {}
    counts = (report or {}).get("classification_counts") or {}
    case_count = int((report or {}).get("case_count") or 0)
    frozen_count = int((report or {}).get("human_frozen_rubric_count") or 0)
    pass_rate = float((report or {}).get("pass_rate") or 0.0)
    checks = {
        "report_provided": isinstance(report, Mapping),
        "report_hash": isinstance(report, Mapping)
        and _verified_report_hash(report, "evaluation_sha256"),
        "component_final": (report or {}).get("version")
        == "hybrid-v2-research-calibration-v1"
        and str((report or {}).get("status") or "").upper() == "FINAL",
        "classification": (report or {}).get("classification")
        == "RESEARCH_CALIBRATION_NOT_PERFORMANCE_HOLDOUT",
        "case_count": case_count >= int(contract.get("minimum_cases") or 20),
        "positive_or_constructive_balance": int(
            counts.get("positive_or_constructive", counts.get("POSITIVE_OR_CONSTRUCTIVE", 0))
        )
        >= int(required_balance.get("positive_or_constructive") or 10),
        "negative_or_ambiguous_balance": int(
            counts.get("negative_or_ambiguous", counts.get("NEGATIVE_OR_AMBIGUOUS", 0))
        )
        >= int(required_balance.get("negative_or_ambiguous") or 10),
        "all_rubrics_human_frozen": case_count > 0 and frozen_count == case_count,
        "rubrics_frozen_before_ai": (report or {}).get("rubrics_frozen_before_ai") is True,
        "future_performance_separated": (report or {}).get(
            "separated_from_future_performance"
        )
        is True,
        "required_pass_rate": pass_rate >= float(contract.get("required_pass_rate") or 1.0),
        "explicit_pass": (report or {}).get("research_calibration_pass") is True,
    }
    return {
        "classification": "RESEARCH_CALIBRATION_NOT_PERFORMANCE_HOLDOUT",
        "status": "MEASURED" if isinstance(report, Mapping) else "N/A_NOT_PROVIDED",
        "checks": checks,
        "passed": all(checks.values()),
        "case_count": case_count,
        "classification_counts": dict(counts),
        "pass_rate": pass_rate,
    }


def _blinded_course_gold_gate(
    report: Mapping[str, Any] | None, protocol: Mapping[str, Any]
) -> dict[str, Any]:
    contract = ((protocol.get("correctness_gate") or {}).get("blinded_course_gold_holdout") or {})
    case_count = int((report or {}).get("case_count") or 0)
    strata = (report or {}).get("stratum_counts") or {}
    atomic_rate = float((report or {}).get("atomic_assertion_agreement_rate") or 0.0)
    permission_rate = float((report or {}).get("material_permission_agreement_rate") or 0.0)
    checks = {
        "report_provided": isinstance(report, Mapping),
        "report_hash": isinstance(report, Mapping)
        and _verified_report_hash(report, "evaluation_sha256"),
        "component_final": (report or {}).get("course_gold_version")
        == "hybrid-v3-course-gold-v2"
        and str((report or {}).get("course_gold_status") or "").upper() == "FINAL",
        "classification": (report or {}).get("classification")
        == "BLINDED_HISTORICAL_COURSE_GOLD_NOT_PERFORMANCE_HOLDOUT",
        "case_count": case_count >= int(contract.get("minimum_cases") or 20),
        "objective_strata": len([key for key, value in strata.items() if int(value) > 0])
        >= int(contract.get("minimum_distinct_objective_strata") or 4),
        "selection_bundle_protocol_bound": (report or {}).get("provenance_pass") is True
        and (report or {}).get("formal_source") is True
        and all(
            isinstance((report or {}).get(field), str)
            and len(str((report or {}).get(field))) == 64
            for field in (
                "selection_plan_sha256",
                "bundle_manifest_sha256",
                "protocol_sha256",
            )
        ),
        "atomic_assertion_agreement": atomic_rate
        >= float(contract.get("required_atomic_assertion_agreement_rate") or 0.9),
        "material_permission_agreement": permission_rate
        >= float(contract.get("required_material_permission_agreement_rate") or 1.0),
        "explicit_pass": (report or {}).get("course_gold_pass") is True,
    }
    return {
        "classification": "BLINDED_HISTORICAL_COURSE_GOLD_NOT_PERFORMANCE_HOLDOUT",
        "status": "MEASURED" if isinstance(report, Mapping) else "N/A_NOT_PROVIDED",
        "checks": checks,
        "passed": all(checks.values()),
        "case_count": case_count,
        "stratum_counts": dict(strata),
        "atomic_assertion_agreement_rate": atomic_rate,
        "material_permission_agreement_rate": permission_rate,
    }


def extract_v1_exclusions(rows: Iterable[Mapping[str, Any]]) -> tuple[set[str], set[str]]:
    review_ids: set[str] = set()
    stock_ids: set[str] = set()
    for row in rows:
        review_id = str(row.get("review_id") or "")
        stock_id = str(row.get("anonymous_stock_id") or "")
        if review_id:
            review_ids.add(review_id)
        if stock_id:
            stock_ids.add(stock_id)
    return review_ids, stock_ids


def _rank(seed: str, stratum: str, row: Mapping[str, Any]) -> str:
    material = "|".join(
        (seed, stratum, str(row["review_id"]), str(row["packet_sha256"]))
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _block(rows: Sequence[dict[str, Any]], block_id: str) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: (int(row["source_ordinal"]), str(row["review_id"])))
    identity = [
        {
            "source_ordinal": row["source_ordinal"],
            "review_id": row["review_id"],
            "anonymous_stock_id": row["anonymous_stock_id"],
            "sampling_stratum": row["sampling_stratum"],
            "packet_sha256": row["packet_sha256"],
        }
        for row in ordered
    ]
    return {
        "block_id": block_id,
        "count": len(identity),
        "stratum_counts": dict(sorted(Counter(row["sampling_stratum"] for row in identity).items())),
        "rows": identity,
        "block_sha256": canonical_sha256(identity),
    }


def build_holdout_plan(
    records: Sequence[Mapping[str, Any]],
    *,
    protocol: Mapping[str, Any],
    v1_rows: Sequence[Mapping[str, Any]] = (),
    outcome_blind_locked: bool,
    source_sha256: str | None = None,
    reserve_block_count: int | None = None,
) -> dict[str, Any]:
    """Deterministically select the primary 120-case holdout and reserves.

    V1 review IDs are hard exclusions.  Anonymous stocks seen by V1 are drawn
    only when a stratum lacks enough stock-disjoint cases.  Primary and reserve
    blocks are mutually review-id-disjoint and retain original source ordinals.
    """

    _assert_outcome_blind_source(records, outcome_blind_locked=outcome_blind_locked)
    consistency = protocol.get("consistency") or {}
    seed = str(consistency.get("seed") or "")
    quotas_raw = consistency.get("strata") or {}
    if not seed or not isinstance(quotas_raw, Mapping) or not quotas_raw:
        raise ConsistencyError("protocol consistency seed/strata are required")
    quotas = {str(key): int(value) for key, value in quotas_raw.items()}
    if any(value <= 0 for value in quotas.values()):
        raise ConsistencyError("all holdout stratum quotas must be positive")
    sample_size = int(consistency.get("sample_size", 0))
    if sample_size != sum(quotas.values()):
        raise ConsistencyError("sample_size differs from the sum of stratum quotas")
    if int(consistency.get("runs", 0)) != 3:
        raise ConsistencyError("protocol V2 consistency requires exactly three runs")
    reserves = int(
        reserve_block_count
        if reserve_block_count is not None
        else consistency.get("reserve_block_count", DEFAULT_RESERVE_BLOCKS)
    )
    if reserves < 1:
        raise ConsistencyError("at least one deterministic reserve block is required")

    normalized = [_normalized_case(row) for row in records]
    ids = [row["review_id"] for row in normalized]
    ordinals = [row["source_ordinal"] for row in normalized]
    if len(ids) != len(set(ids)):
        raise ConsistencyError("source review_id values are not unique")
    if len(ordinals) != len(set(ordinals)):
        raise ConsistencyError("source_ordinal values are not unique")
    v1_review_ids, v1_stock_ids = extract_v1_exclusions(v1_rows)
    after_review_exclusion = [row for row in normalized if row["review_id"] not in v1_review_ids]
    source_identity = sorted(normalized, key=lambda row: row["source_ordinal"])
    computed_source_sha = canonical_sha256(source_identity)
    if source_sha256 is not None:
        _require_sha256(source_sha256, "source_sha256")

    pools: dict[str, list[dict[str, Any]]] = {}
    for stratum in quotas:
        eligible = [row for row in after_review_exclusion if row["sampling_stratum"] == stratum]
        preferred = sorted(
            (row for row in eligible if row["anonymous_stock_id"] not in v1_stock_ids),
            key=lambda row: _rank(seed, stratum, row),
        )
        fallback = sorted(
            (row for row in eligible if row["anonymous_stock_id"] in v1_stock_ids),
            key=lambda row: _rank(seed, stratum, row),
        )
        pools[stratum] = preferred + fallback
        required = quotas[stratum] * (1 + reserves)
        if len(pools[stratum]) < required:
            raise ConsistencyError(
                f"stratum {stratum} has {len(pools[stratum])} eligible cases; {required} required"
            )

    blocks: list[dict[str, Any]] = []
    fallback_counts: dict[str, int] = {}
    for block_index in range(1 + reserves):
        chosen: list[dict[str, Any]] = []
        fallback_count = 0
        for stratum, quota in quotas.items():
            start = block_index * quota
            rows = pools[stratum][start : start + quota]
            chosen.extend(rows)
            fallback_count += sum(row["anonymous_stock_id"] in v1_stock_ids for row in rows)
        block_id = "PRIMARY" if block_index == 0 else f"RESERVE_{block_index:02d}"
        blocks.append(_block(chosen, block_id))
        fallback_counts[block_id] = fallback_count

    all_ids = [row["review_id"] for block in blocks for row in block["rows"]]
    if len(all_ids) != len(set(all_ids)):
        raise ConsistencyError("primary/reserve blocks overlap")
    source_declared_sha = source_sha256.lower() if source_sha256 else computed_source_sha
    plan: dict[str, Any] = {
        "holdout_plan_version": HOLDOUT_PLAN_VERSION,
        "status": "LOCKED_OUTCOME_BLIND_SAMPLE_PLAN",
        "seed": seed,
        "runs": 3,
        "sample_size": sample_size,
        "reserve_block_count": reserves,
        "source_count": len(normalized),
        "source_sha256": source_declared_sha,
        "source_identity_sha256": computed_source_sha,
        "v1_review_id_exclusion_count": len(v1_review_ids),
        "v1_review_ids_sha256": canonical_sha256(sorted(v1_review_ids)),
        "v1_anonymous_stock_exclusion_count": len(v1_stock_ids),
        "v1_anonymous_stock_ids_sha256": canonical_sha256(sorted(v1_stock_ids)),
        "stratum_quotas": quotas,
        "preferred_stock_disjoint": True,
        "stock_disjoint_fallback_counts": fallback_counts,
        "primary": blocks[0],
        "reserve_blocks": blocks[1:],
    }
    plan["plan_sha256"] = canonical_sha256(plan)
    return plan


def select_records_for_block(
    records: Sequence[Mapping[str, Any]], plan: Mapping[str, Any], block_id: str = "PRIMARY"
) -> list[Mapping[str, Any]]:
    blocks = [plan["primary"], *(plan.get("reserve_blocks") or [])]
    selected = next((block for block in blocks if block.get("block_id") == block_id), None)
    if selected is None:
        raise ConsistencyError(f"unknown holdout block: {block_id}")
    by_id = {str(_record_field(row, "review_id")): row for row in records}
    output: list[Mapping[str, Any]] = []
    for identity in selected["rows"]:
        review_id = str(identity["review_id"])
        if review_id not in by_id:
            raise ConsistencyError(f"holdout source row disappeared: {review_id}")
        if _normalized_case(by_id[review_id]) != identity:
            raise ConsistencyError(f"holdout source row changed: {review_id}")
        output.append(by_id[review_id])
    return output


def _run_index(run_rows: Sequence[Mapping[str, Any]], run_number: int) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in run_rows:
        review_id = str(row.get("review_id") or ((row.get("output") or {}).get("review_id")) or "")
        if not review_id:
            raise ConsistencyError(f"run {run_number} contains a row without review_id")
        if review_id in result:
            raise ConsistencyError(f"run {run_number} duplicates review_id {review_id}")
        if row.get("run_number") is not None and int(row["run_number"]) != run_number:
            raise ConsistencyError(f"run_number mismatch for {review_id}")
        result[review_id] = row
    return result


def assert_exact_three_run_coverage(
    sample_rows: Sequence[Mapping[str, Any]],
    runs: Sequence[Sequence[Mapping[str, Any]]],
) -> list[dict[str, Mapping[str, Any]]]:
    if len(runs) != 3:
        raise ConsistencyError("exactly three isolated runs are required")
    expected = {str(row["review_id"]): row for row in sample_rows}
    if len(expected) != len(sample_rows):
        raise ConsistencyError("sample review IDs are not unique")
    indexes: list[dict[str, Mapping[str, Any]]] = []
    for run_number, rows in enumerate(runs, 1):
        index = _run_index(rows, run_number)
        missing = sorted(set(expected) - set(index))
        unexpected = sorted(set(index) - set(expected))
        if missing or unexpected:
            raise ConsistencyError(
                f"run {run_number} coverage mismatch: missing={missing}, unexpected={unexpected}"
            )
        for review_id, identity in expected.items():
            envelope = index[review_id]
            if str(envelope.get("status") or "") != "VALID":
                raise ConsistencyError(f"run {run_number} is not VALID for {review_id}")
            for field in ("source_ordinal", "packet_sha256"):
                if field not in envelope or str(envelope[field]) != str(identity[field]):
                    raise ConsistencyError(f"run {run_number} {field} mismatch for {review_id}")
            output = envelope.get("output")
            if not isinstance(output, Mapping):
                raise ConsistencyError(f"run {run_number} output missing for {review_id}")
            expected_output_sha = _require_sha256(
                envelope.get("output_sha256"), "output_sha256"
            )
            if expected_output_sha != canonical_sha256(output):
                raise ConsistencyError(f"run {run_number} output hash mismatch for {review_id}")
        indexes.append(index)
    return indexes


def _semantic(envelope: Mapping[str, Any]) -> dict[str, Any]:
    value = envelope.get("output", envelope)
    if not isinstance(value, dict):
        raise ConsistencyError("atomic output must be an object")
    return value


AtomKey = tuple[str, str, str]


def _atomic_verdicts(semantic: Mapping[str, Any]) -> dict[AtomKey, Mapping[str, Any]]:
    result: dict[AtomKey, Mapping[str, Any]] = {}
    for question_id, verdict in (semantic.get("global_answers") or {}).items():
        result[("global_answers", "", str(question_id))] = verdict
    candidates = semantic.get("candidate_answers") or {}
    for group in ("anchor_candidates", "relation_candidates", "stop_candidates"):
        for row in candidates.get(group) or []:
            subject_ref = str(row.get("subject_ref") or "")
            for question_id, verdict in (row.get("answers") or {}).items():
                result[(group, subject_ref, str(question_id))] = verdict
    return result


def atom_path(key: AtomKey) -> str:
    group, subject_ref, question_id = key
    return f"{group}[{subject_ref}].{question_id}" if subject_ref else f"{group}.{question_id}"


def _set_verdict(semantic: dict[str, Any], key: AtomKey, verdict: Mapping[str, Any]) -> None:
    group, subject_ref, question_id = key
    if group == "global_answers":
        semantic.setdefault("global_answers", {})[question_id] = dict(verdict)
        return
    candidates = semantic.setdefault("candidate_answers", {}).setdefault(group, [])
    row = next((item for item in candidates if str(item.get("subject_ref")) == subject_ref), None)
    if row is None:
        row = {"subject_ref": subject_ref, "answers": {}}
        candidates.append(row)
    row.setdefault("answers", {})[question_id] = dict(verdict)


def _unknown_from_disagreement(verdicts: Sequence[Mapping[str, Any] | None]) -> dict[str, Any]:
    supporting: set[str] = set()
    contradicting: set[str] = set()
    for verdict in verdicts:
        if not isinstance(verdict, Mapping):
            continue
        supporting.update(str(value) for value in verdict.get("supporting_evidence_refs") or [])
        contradicting.update(str(value) for value in verdict.get("contradicting_evidence_refs") or [])
    # UNKNOWN requires at least one visible ref in the formal schema.  A truly
    # missing field has none, so retain an empty list and let validation fail;
    # the consistency check must not fabricate an evidence ref.
    return {
        "result": "UNKNOWN",
        "supporting_evidence_refs": sorted(supporting),
        "contradicting_evidence_refs": sorted(contradicting),
        "missing_evidence_codes": ["CONFLICTING_VISIBLE_EVIDENCE"],
        "reason_code": "VISIBLE_EVIDENCE_CONFLICTS",
    }


def critical_question_ids(
    protocol: Mapping[str, Any], atomic_schema: Mapping[str, Any] | None = None
) -> set[str]:
    """Read the frozen critical-atom classification; never substitute all atoms."""

    consistency = protocol.get("consistency") or {}
    configured = consistency.get("critical_question_ids")
    if configured is None:
        contract = protocol.get("ai_contract") or {}
        configured = contract.get("critical_question_ids")
    if configured is None and atomic_schema is not None:
        configured = atomic_schema.get("x-critical-question-ids")
        if configured is None:
            configured = atomic_schema.get("x-critical-atoms")
    if not isinstance(configured, list) or not configured:
        raise ConsistencyError(
            "frozen critical question metadata is missing; all atoms cannot be used as a substitute"
        )
    result = {str(value) for value in configured if str(value)}
    if len(result) != len(configured):
        raise ConsistencyError("critical question metadata is empty or duplicated")
    return result


def conservative_merge_atomic_outputs(
    outputs: Sequence[Mapping[str, Any]],
    *,
    critical_ids: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge exactly three outputs; atom disagreement becomes UNKNOWN."""

    if len(outputs) != 3:
        raise ConsistencyError("conservative merge requires exactly three outputs")
    maps = [_atomic_verdicts(output) for output in outputs]
    keys = sorted(set().union(*(set(value) for value in maps)), key=atom_path)
    merged = copy.deepcopy(dict(outputs[0]))
    critical_keys = [key for key in keys if key[2] in critical_ids]
    if not critical_keys:
        raise ConsistencyError("no output atom matches the frozen critical question metadata")
    critical_key_set = set(critical_keys)
    exact = 0
    disagreements: list[str] = []
    critical_by_question: dict[str, dict[str, int | float]] = {
        question_id: {"fields": 0, "exact_fields": 0, "rate": 0.0}
        for question_id in sorted({key[2] for key in critical_keys})
    }
    for key in keys:
        verdicts = [mapping.get(key) for mapping in maps]
        values = [verdict.get("result") if isinstance(verdict, Mapping) else None for verdict in verdicts]
        is_exact = len(verdicts) == 3 and all(value in VERDICTS for value in values) and len(set(values)) == 1
        if is_exact and key in critical_key_set:
            exact += 1
        if key in critical_key_set:
            critical_by_question[key[2]]["fields"] += 1
            critical_by_question[key[2]]["exact_fields"] += int(is_exact)
        if not is_exact:
            disagreements.append(atom_path(key))
            _set_verdict(merged, key, _unknown_from_disagreement(verdicts))
    for row in critical_by_question.values():
        row["rate"] = row["exact_fields"] / row["fields"] if row["fields"] else 0.0
    return merged, {
        "critical_atom_fields": len(critical_keys),
        "critical_atom_exact_fields": exact,
        "critical_atom_rate": exact / len(critical_keys),
        "disagreement_paths": disagreements,
        "critical_disagreement_paths": [
            atom_path(key)
            for key in critical_keys
            if not (
                all((mapping.get(key) or {}).get("result") in VERDICTS for mapping in maps)
                and len({(mapping.get(key) or {}).get("result") for mapping in maps}) == 1
            )
        ],
        "critical_by_question": critical_by_question,
    }


def _decision_phase(decision: Mapping[str, Any]) -> dict[str, Any]:
    derived = decision.get("derived_structure") or {}
    return {
        "scenario": decision.get("scenario", derived.get("primary_scenario")),
        "stage": decision.get("phase", derived.get("stage")),
        "left_right_phase": decision.get("left_right_phase", derived.get("left_right_phase")),
    }


def _action_signature(decision: Mapping[str, Any]) -> Any:
    return decision.get("action_signature")


def _trade_signature_exact(
    decisions: Sequence[Mapping[str, Any]], protocol: Mapping[str, Any]
) -> bool:
    fields = ((protocol.get("policy") or {}).get("material_action_signature_fields") or [])
    if not isinstance(fields, list) or not fields:
        raise ConsistencyError("material_action_signature_fields metadata is missing")
    signatures: list[dict[str, Any]] = []
    unresolved = {None, "", "UNKNOWN", "UNRESOLVED"}
    for decision in decisions:
        source = _action_signature(decision)
        if not isinstance(source, Mapping):
            return False
        selected = {str(field): source.get(str(field)) for field in fields}
        if any(value in unresolved for value in selected.values()):
            return False
        signatures.append(selected)
    return _agreement(signatures)


def _agreement(values: Sequence[Any]) -> bool:
    return len(values) == 3 and len({canonical_sha256(value) for value in values}) == 1


def _operational_metrics(runs: Sequence[Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    rows = [row for run in runs for row in run]
    successes = sum(str(row.get("status") or "VALID") == "VALID" for row in rows)
    attempts = [max(1, int(row.get("successful_attempt", row.get("attempt_count", 1)))) for row in rows]
    elapsed = [
        float(row[field])
        for row in rows
        for field in ("elapsed_seconds", "duration_seconds")
        if row.get(field) is not None and math.isfinite(float(row[field]))
    ]
    return {
        "expected_envelopes": len(rows),
        "valid_envelopes": successes,
        "completion_rate": successes / len(rows) if rows else 0.0,
        "total_attempts": sum(attempts),
        "retry_count": sum(value - 1 for value in attempts),
        "terminal_failure_count": len(rows) - successes,
        "elapsed_observation_count": len(elapsed),
        "elapsed_mean_seconds": statistics.fmean(elapsed) if elapsed else None,
        "elapsed_median_seconds": statistics.median(elapsed) if elapsed else None,
        "elapsed_max_seconds": max(elapsed) if elapsed else None,
    }


def evaluate_consistency(
    *,
    records: Sequence[Mapping[str, Any]],
    sample_block: Mapping[str, Any],
    runs: Sequence[Sequence[Mapping[str, Any]]],
    protocol: Mapping[str, Any],
    validate_fn: ValidateFn,
    reduce_fn: ReduceFn,
    course_invariant_fn: CourseInvariantFn | None = None,
    gold_cases: Mapping[str, Mapping[str, Any]] | None = None,
    research_calibration_report: Mapping[str, Any] | None = None,
    blinded_course_gold_report: Mapping[str, Any] | None = None,
    atomic_schema: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate repeatability without treating it as course truth or performance.

    Optional course invariants and independently prepared gold cases are reported
    in separate sections.  Neither may contain or score future P/L, and neither
    changes the repeatability gate.
    """

    sample_rows = list(sample_block.get("rows") or [])
    if int(sample_block.get("count", len(sample_rows))) != len(sample_rows):
        raise ConsistencyError("sample block count is inconsistent")
    rebuilt_block = _block(sample_rows, str(sample_block.get("block_id") or "PRIMARY"))
    if sample_block.get("block_sha256") != rebuilt_block["block_sha256"]:
        raise ConsistencyError("sample block hash is missing or changed")
    indexes = assert_exact_three_run_coverage(sample_rows, runs)
    source_by_id = {str(_record_field(row, "review_id")): row for row in records}
    critical_ids = critical_question_ids(protocol, atomic_schema)
    validation_slots = merged_validation_slots = agreement_permission = agreement_phase = 0
    critical_total = critical_exact = 0
    critical_question_totals: Counter[str] = Counter()
    critical_question_exact: Counter[str] = Counter()
    trade_signature_total = trade_signature_exact = 0
    trade_route_exact = exact_wait_cases = exact_remove_cases = 0
    conservative_permissions: Counter[str] = Counter()
    positive_routes: Counter[str] = Counter()
    cases: list[dict[str, Any]] = []
    course_invariant_errors: dict[str, list[str]] = {}
    gold_results: list[dict[str, Any]] = []
    if gold_cases is not None:
        allowed_gold_fields = {
            "permission",
            "route",
            "scenario",
            "action_signature",
            "critical_atoms",
        }
        for review_id, expected in gold_cases.items():
            unknown = set(expected) - allowed_gold_fields
            if unknown:
                raise ConsistencyError(
                    f"gold case {review_id} has non-semantic or forbidden fields: {sorted(unknown)}"
                )

    for identity in sample_rows:
        review_id = str(identity["review_id"])
        source = source_by_id.get(review_id)
        if source is None:
            raise ConsistencyError(f"packet source missing for {review_id}")
        if _normalized_case(source) != identity:
            raise ConsistencyError(f"packet source changed after sampling: {review_id}")
        packet = source.get("packet", source)
        outputs = [_semantic(index[review_id]) for index in indexes]
        decisions: list[dict[str, Any]] = []
        validation_errors: list[list[str]] = []
        for output in outputs:
            errors = list(validate_fn(packet, output))
            validation_errors.append(errors)
            if not errors:
                validation_slots += 1
            decision = reduce_fn(packet, output)
            if not isinstance(decision, dict):
                raise ConsistencyError("atomic policy reducer must return an object")
            if decision.get("permission") not in {"TRADE", "WAIT", "REMOVE"}:
                raise ConsistencyError(
                    f"atomic policy returned invalid permission for {review_id}: "
                    f"{decision.get('permission')!r}"
                )
            decisions.append(decision)

        permissions = [decision.get("permission") for decision in decisions]
        phases = [_decision_phase(decision) for decision in decisions]
        permission_exact = _agreement(permissions)
        phase_exact = _agreement(phases)
        agreement_permission += int(permission_exact)
        agreement_phase += int(phase_exact)
        if permissions == ["TRADE", "TRADE", "TRADE"]:
            trade_signature_total += 1
            trade_signature_exact += int(_trade_signature_exact(decisions, protocol))
            trade_route_exact += int(_agreement([row.get("route") for row in decisions]))
        exact_wait_cases += int(permissions == ["WAIT", "WAIT", "WAIT"])
        exact_remove_cases += int(permissions == ["REMOVE", "REMOVE", "REMOVE"])

        merged, atom_metrics = conservative_merge_atomic_outputs(
            outputs, critical_ids=critical_ids
        )
        critical_total += int(atom_metrics["critical_atom_fields"])
        critical_exact += int(atom_metrics["critical_atom_exact_fields"])
        for question_id, row in atom_metrics["critical_by_question"].items():
            critical_question_totals[question_id] += int(row["fields"])
            critical_question_exact[question_id] += int(row["exact_fields"])
        merged_validation_errors = list(validate_fn(packet, merged))
        merged_validation_slots += int(not merged_validation_errors)
        merged_decision = reduce_fn(packet, merged)
        permission = str(merged_decision.get("permission") or "WAIT")
        if permission not in {"TRADE", "WAIT", "REMOVE"}:
            raise ConsistencyError(f"invalid conservative permission for {review_id}: {permission}")
        conservative_permissions[permission] += 1
        if permission == "TRADE":
            positive_routes[str(merged_decision.get("route") or "UNRESOLVED")] += 1
        if course_invariant_fn is not None:
            invariant_errors = list(course_invariant_fn(packet, merged, merged_decision))
            if invariant_errors:
                course_invariant_errors[review_id] = invariant_errors
        if gold_cases is not None and review_id in gold_cases:
            expected = dict(gold_cases[review_id])
            actual: dict[str, Any] = {}
            for field in ("permission", "route", "scenario", "action_signature"):
                if field in expected:
                    actual[field] = merged_decision.get(field)
            if "critical_atoms" in expected:
                actual["critical_atoms"] = {
                    atom_path(key): verdict.get("result")
                    for key, verdict in _atomic_verdicts(merged).items()
                    if atom_path(key) in set(expected["critical_atoms"])
                }
            gold_results.append(
                {
                    "review_id": review_id,
                    "expected": expected,
                    "actual": actual,
                    "exact": canonical_sha256(expected) == canonical_sha256(actual),
                }
            )
        cases.append(
            {
                "source_ordinal": identity["source_ordinal"],
                "review_id": review_id,
                "sampling_stratum": identity["sampling_stratum"],
                "run_validation_errors": validation_errors,
                "permissions": permissions,
                "permission_exact": permission_exact,
                "scenario_phase_exact": phase_exact,
                "critical_atom_rate": atom_metrics["critical_atom_rate"],
                "critical_atom_disagreements": atom_metrics["disagreement_paths"],
                "critical_atom_critical_disagreements": atom_metrics[
                    "critical_disagreement_paths"
                ],
                "conservative_validation_errors": merged_validation_errors,
                "conservative_decision": merged_decision,
            }
        )

    count = len(sample_rows)
    slots = count * 3
    consistency = protocol.get("consistency") or {}
    rates = {
        "schema_causality_evidence_rate": validation_slots / slots if slots else 0.0,
        "conservative_schema_causality_evidence_rate": (
            merged_validation_slots / count if count else 0.0
        ),
        "permission_rate": agreement_permission / count if count else 0.0,
        "scenario_phase_rate": agreement_phase / count if count else 0.0,
        "critical_atom_rate": critical_exact / critical_total if critical_total else 0.0,
        "trade_action_signature_rate": (
            trade_signature_exact / trade_signature_total if trade_signature_total else 0.0
        ),
    }
    remove_eligible = sum(
        row["sampling_stratum"] == "MACRO_DEFENSE_REMOVE_PROXY" for row in sample_rows
    )
    eligible_remove_count = sum(
        row["sampling_stratum"] == "MACRO_DEFENSE_REMOVE_PROXY"
        and row["conservative_decision"].get("permission") == "REMOVE"
        for row in cases
    )
    checks = {
        "exact_three_run_coverage": True,
        "schema_causality_evidence": rates["schema_causality_evidence_rate"]
        >= float(consistency["schema_causality_evidence_rate"]),
        "conservative_schema_causality_evidence": rates[
            "conservative_schema_causality_evidence_rate"
        ]
        >= float(consistency["schema_causality_evidence_rate"]),
        "permission": rates["permission_rate"] >= float(consistency["permission_rate"]),
        "scenario_phase": rates["scenario_phase_rate"] >= float(consistency["scenario_phase_rate"]),
        "critical_atoms": rates["critical_atom_rate"] >= float(consistency["critical_atom_rate"]),
        "trade_action_signature": rates["trade_action_signature_rate"]
        >= float(consistency["trade_action_signature_rate"]),
        "minimum_conservative_trade_cases": conservative_permissions["TRADE"]
        >= int(consistency["minimum_conservative_trade_cases"]),
        "minimum_trade_routes": len(positive_routes) >= int(consistency["minimum_trade_routes"]),
        "minimum_wait_cases": conservative_permissions["WAIT"]
        >= int(consistency["minimum_wait_cases"]),
        "minimum_remove_cases_when_eligible": (
            True
            if remove_eligible == 0
            else eligible_remove_count
            >= int(consistency["minimum_remove_cases_when_eligible"])
        ),
        "not_all_wait": conservative_permissions["WAIT"] != count
        and conservative_permissions["TRADE"] > 0,
    }
    course_correctness: dict[str, Any]
    if course_invariant_fn is None:
        course_correctness = {
            "status": "N/A_NO_COURSE_INVARIANT_REVIEW_PROVIDED",
            "passed": None,
            "note": "Three-run repeatability is not proof of course correctness.",
        }
    else:
        course_correctness = {
            "status": "MEASURED_WITH_NON_PERFORMANCE_COURSE_INVARIANTS",
            "checked_cases": count,
            "passing_cases": count - len(course_invariant_errors),
            "pass_rate": (count - len(course_invariant_errors)) / count if count else 0.0,
            "passed": count > 0 and not course_invariant_errors,
            "errors_by_review_id": course_invariant_errors,
        }
    if gold_cases is None:
        gold_report: dict[str, Any] = {
            "status": "N/A_NO_INDEPENDENT_GOLD_CASES_PROVIDED",
            "passed": None,
            "classification": "RESEARCH_CALIBRATION_NOT_PERFORMANCE_HOLDOUT",
            "note": "Old AI outputs and future performance are not ground truth; course-correctness gold acceptance is incomplete.",
        }
    else:
        gold_exact = sum(row["exact"] for row in gold_results)
        gold_report = {
            "status": "MEASURED_AGAINST_INDEPENDENT_NON_PERFORMANCE_GOLD_CASES",
            "classification": "RESEARCH_CALIBRATION_NOT_PERFORMANCE_HOLDOUT",
            "provided_cases": len(gold_cases),
            "covered_cases": len(gold_results),
            "exact_cases": gold_exact,
            "exact_rate": gold_exact / len(gold_results) if gold_results else 0.0,
            "cases": gold_results,
        }
        gold_threshold = consistency.get("gold_case_rate")
        gold_report["passed"] = (
            len(gold_results) == len(gold_cases)
            and bool(gold_results)
            and (
                gold_report["exact_rate"] >= float(gold_threshold)
                if gold_threshold is not None
                else True
            )
        )
    repeatability_pass = all(checks.values())
    gold_required = consistency.get("gold_case_rate") is not None
    course_pass = course_correctness.get("passed") is True
    gold_pass = gold_report.get("passed") is True if gold_required else True
    research_gate = _research_calibration_gate(research_calibration_report, protocol)
    blinded_gold_gate = _blinded_course_gold_gate(blinded_course_gold_report, protocol)
    # The legacy optional `gold_cases` argument is diagnostic only.  It cannot
    # satisfy either of the two independent frozen correctness gates.
    formal_acceptance_inputs_pass = (
        repeatability_pass
        and course_pass
        and gold_pass
        and research_gate["passed"]
        and blinded_gold_gate["passed"]
    )
    overall_formal_pass = formal_acceptance_inputs_pass and CONSISTENCY_STATUS == "FINAL"
    critical_by_question = {
        question_id: {
            "fields": critical_question_totals[question_id],
            "exact_fields": critical_question_exact[question_id],
            "rate": critical_question_exact[question_id]
            / critical_question_totals[question_id],
        }
        for question_id in sorted(critical_question_totals)
    }
    report = {
        "consistency_version": CONSISTENCY_VERSION,
        "consistency_status": CONSISTENCY_STATUS,
        "sample_block_id": sample_block.get("block_id"),
        "sample_block_sha256": sample_block.get("block_sha256"),
        "sample_size": count,
        "runs": 3,
        "rates": rates,
        "agreement_counts": {
            "valid_run_outputs": validation_slots,
            "run_output_slots": slots,
            "valid_conservative_outputs": merged_validation_slots,
            "permission_exact_cases": agreement_permission,
            "scenario_phase_exact_cases": agreement_phase,
            "critical_atom_exact_fields": critical_exact,
            "critical_atom_fields": critical_total,
            "comparable_trade_cases": trade_signature_total,
            "trade_action_signature_exact_cases": trade_signature_exact,
        },
        "critical_atom_by_question": critical_by_question,
        "conservative_permission_counts": dict(
            sorted({key: conservative_permissions[key] for key in ("TRADE", "WAIT", "REMOVE")}.items())
        ),
        "positive_route_counts": dict(sorted(positive_routes.items())),
        "permission_route_detail": {
            "three_run_trade_cases": trade_signature_total,
            "three_run_positive_route_exact_cases": trade_route_exact,
            "three_run_positive_route_rate": (
                trade_route_exact / trade_signature_total if trade_signature_total else 0.0
            ),
            "three_run_wait_exact_cases": exact_wait_cases,
            "three_run_remove_exact_cases": exact_remove_cases,
        },
        "remove_eligible_cases": remove_eligible,
        "eligible_remove_cases": eligible_remove_count,
        "operational_reliability": _operational_metrics(runs),
        "three_run_repeatability": {
            "rates": rates,
            "checks": checks,
            "passed": repeatability_pass,
        },
        "course_correctness": course_correctness,
        "gold_case_agreement": gold_report,
        "research_calibration": research_gate,
        "blinded_course_gold_holdout": blinded_gold_gate,
        "future_performance": {
            "status": "LOCKED_PENDING_FULL_SEMANTIC_LEDGER",
            "note": "Performance remains hidden until the complete common semantic ledger is locked and hashed.",
        },
        "correctness_layers": {
            "L0_CAUSAL_DATA_INTEGRITY": {
                "passed": checks["exact_three_run_coverage"]
                and checks["schema_causality_evidence"]
                and checks["conservative_schema_causality_evidence"]
            },
            "L1_DETERMINISTIC_COURSE_INVARIANT": {"passed": course_pass},
            "L2_RESEARCH_CALIBRATION_SEMANTIC_REPRODUCTION": {
                "passed": research_gate["passed"]
            },
            "L3_BLINDED_COURSE_GOLD_HOLDOUT": {"passed": blinded_gold_gate["passed"]},
            "L4_THREE_RUN_REPEATABILITY": {"passed": repeatability_pass},
            "L5_FORWARD_PERFORMANCE_VALIDATION": {
                "passed": None,
                "status": "LOCKED_PENDING_FULL_SEMANTIC_LEDGER",
            },
        },
        "thresholds": dict(consistency),
        "checks": checks,
        "repeatability_pass": repeatability_pass,
        "course_correctness_pass": course_correctness.get("passed"),
        "gold_case_pass": gold_report.get("passed"),
        "research_calibration_pass": research_gate["passed"],
        "blinded_course_gold_pass": blinded_gold_gate["passed"],
        "formal_acceptance_inputs_pass": formal_acceptance_inputs_pass,
        "consistency_component_final": CONSISTENCY_STATUS == "FINAL",
        "overall_formal_pass": overall_formal_pass,
        "passed": overall_formal_pass,
        "cases": sorted(cases, key=lambda row: int(row["source_ordinal"])),
    }
    report["report_sha256"] = canonical_sha256(report)
    return report


def _python_constants(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FormalReadinessError(f"formal dependency is missing: {path}")
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise FormalReadinessError(f"invalid formal dependency {path}: {exc}") from exc
    values: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    values[target.id] = value
    return values


def assert_formal_dependencies(
    *, policy_path: Path = DEFAULT_POLICY, packet_builder_path: Path = DEFAULT_PACKET_BUILDER
) -> dict[str, Any]:
    requirements = (
        (policy_path, "hybrid-v3-atomic-policy-v2", ("POLICY_VERSION", "COMPONENT_VERSION"), ("POLICY_STATUS", "COMPONENT_STATUS")),
        (packet_builder_path, "hybrid-v3-atomic-packets-v2", ("BUILDER_VERSION", "PACKET_BUILDER_VERSION", "COMPONENT_VERSION"), ("BUILDER_STATUS", "PACKET_BUILDER_STATUS", "COMPONENT_STATUS")),
    )
    result: dict[str, Any] = {}
    for path, expected, version_keys, status_keys in requirements:
        constants = _python_constants(path)
        version = next((constants[key] for key in version_keys if key in constants), None)
        status = next((constants[key] for key in status_keys if key in constants), None)
        if version != expected:
            raise FormalReadinessError(f"{path.name} version must be {expected}, got {version!r}")
        if str(status or "").upper() != "FINAL":
            raise FormalReadinessError(f"{path.name} status must be FINAL, got {status!r}")
        result[path.name] = {"version": version, "status": "FINAL", "sha256": file_sha256(path)}
    return result


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ConsistencyError(f"expected JSON object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def _publish_immutable(path: Path, value: Mapping[str, Any]) -> None:
    payload = canonical_json_bytes(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise ConsistencyError(f"immutable consistency artifact already differs: {path}")
        return
    with path.open("xb") as handle:
        handle.write(payload)


def _load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise FormalReadinessError(f"cannot import formal dependency: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--packet-builder", type=Path, default=DEFAULT_PACKET_BUILDER)
    parser.add_argument("--source", type=Path, required=True, help="Outcome-blind runner case-record JSONL")
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--v1-sample", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reserve-blocks", type=int)
    parser.add_argument("--run", type=Path, action="append", default=[])
    parser.add_argument("--research-calibration-report", type=Path)
    parser.add_argument("--blinded-course-gold-report", type=Path)
    args = parser.parse_args()

    dependencies = assert_formal_dependencies(
        policy_path=args.policy, packet_builder_path=args.packet_builder
    )
    protocol = _read_json(args.protocol)
    manifest = _read_json(args.source_manifest)
    if str(manifest.get("status") or "").upper() != "LOCKED_OUTCOME_BLIND":
        raise FormalReadinessError("source manifest must be LOCKED_OUTCOME_BLIND")
    source_sha = file_sha256(args.source)
    if str(manifest.get("artifact_sha256") or manifest.get("source_sha256") or "").lower() != source_sha:
        raise FormalReadinessError("source artifact hash differs from its outcome-blind manifest")
    records = _read_jsonl(args.source)
    v1_rows = _read_jsonl(args.v1_sample)
    v1_review_ids, _ = extract_v1_exclusions(v1_rows)
    if len(v1_review_ids) != 60:
        raise FormalReadinessError(
            f"formal V1 exclusion must contain exactly 60 review_id values, got {len(v1_review_ids)}"
        )
    plan = build_holdout_plan(
        records,
        protocol=protocol,
        v1_rows=v1_rows,
        outcome_blind_locked=True,
        source_sha256=source_sha,
        reserve_block_count=args.reserve_blocks,
    )
    plan.pop("plan_sha256", None)
    plan["formal_dependencies"] = dependencies
    plan["plan_sha256"] = canonical_sha256(plan)
    if not args.run:
        _publish_immutable(args.output, plan)
        print(json.dumps({"mode": "HOLDOUT", "output": str(args.output.resolve()), "plan_sha256": plan["plan_sha256"]}, ensure_ascii=False, indent=2))
        return 0
    if len(args.run) != 3:
        parser.error("formal consistency evaluation requires exactly three --run files")
    if args.research_calibration_report is None or args.blinded_course_gold_report is None:
        parser.error(
            "formal consistency evaluation requires both --research-calibration-report "
            "and --blinded-course-gold-report"
        )
    policy = _load_module(args.policy, "hybrid_v3_policy_v2_formal")
    validate_fn = getattr(policy, "validate_atomic", None)
    reduce_fn = getattr(policy, "reduce_atomic_v3", None)
    course_invariant_fn = getattr(policy, "validate_course_invariants", None)
    if not callable(validate_fn) or not callable(reduce_fn):
        raise FormalReadinessError("formal policy must export validate_atomic and reduce_atomic_v3")
    if not callable(course_invariant_fn):
        raise FormalReadinessError(
            "formal policy must export deterministic validate_course_invariants"
        )
    report = evaluate_consistency(
        records=records,
        sample_block=plan["primary"],
        runs=[_read_jsonl(path) for path in args.run],
        protocol=protocol,
        validate_fn=validate_fn,
        reduce_fn=reduce_fn,
        course_invariant_fn=course_invariant_fn,
        research_calibration_report=_read_json(args.research_calibration_report),
        blinded_course_gold_report=_read_json(args.blinded_course_gold_report),
        atomic_schema=_read_json(args.schema),
    )
    report["holdout_plan_sha256"] = plan["plan_sha256"]
    report["formal_dependencies"] = dependencies
    report.pop("report_sha256", None)
    report["report_sha256"] = canonical_sha256(report)
    _publish_immutable(args.output, report)
    print(json.dumps({"mode": "EVALUATE", "output": str(args.output.resolve()), "passed": report["passed"], "report_sha256": report["report_sha256"]}, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(_main())
