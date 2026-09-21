"""Stream and audit two outcome-blind hybrid review-point universes.

The audit is intentionally performance-blind.  It verifies that a candidate
enumeration change did not alter stock/day alignment or the causal market and
structure evidence supplied to the semantic reviewer.  Enumeration-derived
fields are reported separately rather than silently ignored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from itertools import zip_longest
from pathlib import Path
from typing import Any, Iterable


ENUMERATION_DERIVED_OBJECTIVE_FIELDS = {
    "ai_visible_evidence_sha256",
    "builder_version",
    "candidate_enumeration_policy",
    "completed_prior_copy_count",
    "direct_same_clean_impulse",
    "first_retest_after_large_break_held",
    "material_objective_hypothesis_conflict",
    "material_objective_signature_count",
    "position_role",
    "same_direction_attack_number",
    "scenario_hypotheses",
    "scenario_hypothesis_counts",
    "taiji_generation",
    "data_sufficiency_by_route",
    "wait_boundary_reasons",
}

ENUMERATION_EVIDENCE_KINDS = {"ANCHOR_CANDIDATE", "RELATION_CANDIDATE"}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _rows(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSONL at {path}:{line_number}") from exc


def audit(left_path: Path, right_path: Path) -> dict[str, Any]:
    objective_diff_counts: Counter[str] = Counter()
    evidence_kind_counts: Counter[str] = Counter()
    evidence_mismatch_kind_counts: Counter[str] = Counter()
    left_only_anchor_candidates: Counter[bytes] = Counter()
    right_only_anchor_candidates: Counter[bytes] = Counter()
    left_only_relations: Counter[bytes] = Counter()
    right_only_relations: Counter[bytes] = Counter()
    right_only_nonfresh_relations: Counter[bytes] = Counter()
    top_level_diff_counts: Counter[str] = Counter()
    row_count = 0
    alignment_errors: list[dict[str, Any]] = []
    evidence_mismatch_examples: list[dict[str, Any]] = []
    invariant_objective_mismatch_examples: list[dict[str, Any]] = []
    invariant_objective_mismatch_rows = 0
    evidence_mismatch_rows = 0

    sentinel = object()
    for left, right in zip_longest(_rows(left_path), _rows(right_path), fillvalue=sentinel):
        if left is sentinel or right is sentinel:
            alignment_errors.append(
                {
                    "row_index": row_count,
                    "reason": "ROW_COUNT_MISMATCH",
                    "left_present": left is not sentinel,
                    "right_present": right is not sentinel,
                }
            )
            break
        assert isinstance(left, dict) and isinstance(right, dict)
        row_count += 1
        left_packet = left["packet"]
        right_packet = right["packet"]
        identity = {
            "source_ordinal": left.get("source_ordinal"),
            "anonymous_stock_id": left.get("anonymous_stock_id"),
            "as_of": left_packet.get("as_of"),
        }
        right_identity = {
            "source_ordinal": right.get("source_ordinal"),
            "anonymous_stock_id": right.get("anonymous_stock_id"),
            "as_of": right_packet.get("as_of"),
        }
        if identity != right_identity and len(alignment_errors) < 20:
            alignment_errors.append(
                {
                    "row_index": row_count - 1,
                    "reason": "IDENTITY_OR_AS_OF_MISMATCH",
                    "left": identity,
                    "right": right_identity,
                }
            )

        for key in sorted(set(left) | set(right)):
            if left.get(key, sentinel) != right.get(key, sentinel):
                top_level_diff_counts[key] += 1

        left_evidence = left_packet.get("evidence") or []
        right_evidence = right_packet.get("evidence") or []
        for item in right_evidence or []:
            evidence_kind_counts[str(item.get("kind"))] += 1
        left_base_evidence = [
            item for item in left_evidence if item.get("kind") not in ENUMERATION_EVIDENCE_KINDS
        ]
        right_base_evidence = [
            item for item in right_evidence if item.get("kind") not in ENUMERATION_EVIDENCE_KINDS
        ]
        if left_base_evidence != right_base_evidence:
            evidence_mismatch_rows += 1
            left_by_ref = {item.get("ref"): item for item in left_base_evidence}
            right_by_ref = {item.get("ref"): item for item in right_base_evidence}
            differing_refs = [
                ref
                for ref in sorted(set(left_by_ref) | set(right_by_ref), key=str)
                if left_by_ref.get(ref, sentinel) != right_by_ref.get(ref, sentinel)
            ]
            for ref in differing_refs:
                item = right_by_ref.get(ref) or left_by_ref.get(ref) or {}
                evidence_mismatch_kind_counts[str(item.get("kind"))] += 1
            if len(evidence_mismatch_examples) < 10:
                evidence_mismatch_examples.append(
                    {
                        **identity,
                        "left_evidence_sha256": _sha256(left_base_evidence),
                        "right_evidence_sha256": _sha256(right_base_evidence),
                        "differing_refs": differing_refs[:20],
                    }
                )

        def candidate_counters(evidence: list[dict[str, Any]]) -> tuple[Counter[bytes], Counter[bytes]]:
            anchors: Counter[bytes] = Counter()
            relations: Counter[bytes] = Counter()
            for item in evidence:
                kind = item.get("kind")
                if kind == "ANCHOR_CANDIDATE":
                    anchors[_canonical({"date": item.get("date"), "values": item.get("values")})] += 1
                elif kind == "RELATION_CANDIDATE":
                    values = dict(item.get("values") or {})
                    values.pop("enumeration_policy", None)
                    relations[_canonical({"date": item.get("date"), "values": values})] += 1
            return anchors, relations

        left_anchors, left_relations = candidate_counters(left_evidence)
        right_anchors, right_relations = candidate_counters(right_evidence)
        for value, count in (left_anchors - right_anchors).items():
            left_only_anchor_candidates[value] += count
        for value, count in (right_anchors - left_anchors).items():
            right_only_anchor_candidates[value] += count
        for value, count in (left_relations - right_relations).items():
            left_only_relations[value] += count
        for value, count in (right_relations - left_relations).items():
            right_only_relations[value] += count
            decoded = json.loads(value)
            relation_values = decoded.get("values") or {}
            if relation_values.get("eligible_scenarios") != ["FRESH_Q1_EXPANSION"]:
                right_only_nonfresh_relations[value] += count

        left_objective = left_packet.get("objective_facts") or {}
        right_objective = right_packet.get("objective_facts") or {}
        invariant_differences: list[str] = []
        for key in sorted(set(left_objective) | set(right_objective)):
            if left_objective.get(key, sentinel) != right_objective.get(key, sentinel):
                objective_diff_counts[key] += 1
                if key not in ENUMERATION_DERIVED_OBJECTIVE_FIELDS:
                    invariant_differences.append(key)
        if invariant_differences:
            invariant_objective_mismatch_rows += 1
            if len(invariant_objective_mismatch_examples) < 10:
                invariant_objective_mismatch_examples.append(
                    {**identity, "differing_fields": invariant_differences}
                )

    invariant_pass = (
        not alignment_errors
        and evidence_mismatch_rows == 0
        and invariant_objective_mismatch_rows == 0
        and not left_only_anchor_candidates
        and not left_only_relations
        and not right_only_nonfresh_relations
    )
    return {
        "audit_version": "hybrid-v3-candidate-diff-audit-v1",
        "status": "PASS" if invariant_pass else "FAIL",
        "outcome_blind": True,
        "performance_or_future_fields_read": False,
        "left": str(left_path.resolve()),
        "right": str(right_path.resolve()),
        "rows_compared": row_count,
        "alignment_errors": alignment_errors,
        "causal_evidence": {
            "definition": "all evidence except enumeration-only ANCHOR_CANDIDATE and RELATION_CANDIDATE",
            "exact_match": evidence_mismatch_rows == 0,
            "mismatch_rows": evidence_mismatch_rows,
            "right_kind_counts": dict(sorted(evidence_kind_counts.items())),
            "mismatch_kind_counts": dict(sorted(evidence_mismatch_kind_counts.items())),
            "examples": evidence_mismatch_examples,
        },
        "enumeration_evidence": {
            "normalization": "relation enumeration_policy and evidence ref excluded; anchor values retained",
            "left_only_anchor_count": sum(left_only_anchor_candidates.values()),
            "right_only_anchor_count": sum(right_only_anchor_candidates.values()),
            "left_only_relation_count": sum(left_only_relations.values()),
            "right_only_relation_count": sum(right_only_relations.values()),
            "right_only_nonfresh_relation_count": sum(right_only_nonfresh_relations.values()),
            "right_only_relation_types": dict(
                sorted(
                    Counter(
                        str((json.loads(value).get("values") or {}).get("relation_type"))
                        for value, count in right_only_relations.items()
                        for _ in range(count)
                    ).items()
                )
            ),
        },
        "objective_facts": {
            "invariant_fields_exact_match": invariant_objective_mismatch_rows == 0,
            "invariant_mismatch_rows": invariant_objective_mismatch_rows,
            "enumeration_derived_fields": sorted(ENUMERATION_DERIVED_OBJECTIVE_FIELDS),
            "all_field_diff_counts": dict(sorted(objective_diff_counts.items())),
            "invariant_mismatch_examples": invariant_objective_mismatch_examples,
        },
        "top_level_diff_counts": dict(sorted(top_level_diff_counts.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left", type=Path, required=True)
    parser.add_argument("--right", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    result = audit(args.left, args.right)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": result["status"],
                "rows_compared": result["rows_compared"],
                "evidence_mismatch_rows": result["causal_evidence"]["mismatch_rows"],
                "invariant_objective_mismatch_rows": result["objective_facts"]["invariant_mismatch_rows"],
                "output": str(args.output.resolve()),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
