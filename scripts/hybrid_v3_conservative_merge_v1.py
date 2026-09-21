"""Schema-valid conservative merge for three V3 atomic AI outputs.

The earlier consistency component correctly downgraded every disagreement to
UNKNOWN, but could place one evidence ref in both the supporting and
contradicting lists when different runs interpreted that same visible fact in
opposite ways.  The atomic schema forbids that overlap.  This version preserves
each visible ref exactly once and records the ambiguity through
CONFLICTING_VISIBLE_EVIDENCE.  It does not select a majority verdict and does
not alter the V3 policy reducer.
"""
from __future__ import annotations

import copy
from typing import Any, Mapping, Sequence

try:
    from .hybrid_v3_consistency_v2 import _atomic_verdicts, _set_verdict, atom_path
except ImportError:  # direct script import
    from scripts.hybrid_v3_consistency_v2 import _atomic_verdicts, _set_verdict, atom_path


MERGE_VERSION = "hybrid-v3-conservative-merge-v1"
MERGE_STATUS = "FINAL"
VERDICTS = {"PASS", "FAIL", "UNKNOWN"}


class ConservativeMergeError(ValueError):
    """The three-run output set cannot be conservatively merged."""


def unknown_from_disagreement(
    verdicts: Sequence[Mapping[str, Any] | None],
) -> dict[str, Any]:
    """Build one valid UNKNOWN without fabricating or duplicating evidence.

    A ref interpreted both ways is retained once in the supporting list solely
    as visible conflict context.  The verdict remains UNKNOWN and therefore
    cannot grant a trading route.
    """

    supporting: set[str] = set()
    contradicting: set[str] = set()
    for verdict in verdicts:
        if not isinstance(verdict, Mapping):
            continue
        supporting.update(str(value) for value in verdict.get("supporting_evidence_refs") or [])
        contradicting.update(
            str(value) for value in verdict.get("contradicting_evidence_refs") or []
        )
    overlap = supporting & contradicting
    supporting_only = supporting - contradicting
    contradicting_only = contradicting - supporting
    # UNKNOWN requires at least one visible ref.  Keep overlapping refs once;
    # their conflicting interpretation is explicitly captured by the code.
    merged_supporting = supporting_only | overlap
    if not merged_supporting and not contradicting_only:
        raise ConservativeMergeError(
            "disagreeing verdicts contain no visible evidence ref; cannot fabricate one"
        )
    return {
        "result": "UNKNOWN",
        "supporting_evidence_refs": sorted(merged_supporting),
        "contradicting_evidence_refs": sorted(contradicting_only),
        "missing_evidence_codes": ["CONFLICTING_VISIBLE_EVIDENCE"],
        "reason_code": "VISIBLE_EVIDENCE_CONFLICTS",
    }


def conservative_merge_atomic_outputs(
    outputs: Sequence[Mapping[str, Any]],
    *,
    critical_ids: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge exactly three outputs; every result disagreement becomes UNKNOWN."""

    if len(outputs) != 3:
        raise ConservativeMergeError("conservative merge requires exactly three outputs")
    maps = [_atomic_verdicts(output) for output in outputs]
    keys = sorted(set().union(*(set(value) for value in maps)), key=atom_path)
    if any(set(mapping) != set(keys) for mapping in maps):
        raise ConservativeMergeError("three outputs do not cover the same atomic manifest")
    merged = copy.deepcopy(dict(outputs[0]))
    critical_keys = [key for key in keys if key[2] in critical_ids]
    if not critical_keys:
        raise ConservativeMergeError("no output atom matches the frozen critical metadata")
    critical_key_set = set(critical_keys)
    exact = 0
    disagreements: list[str] = []
    critical_disagreements: list[str] = []
    critical_by_question: dict[str, dict[str, int | float]] = {
        question_id: {"fields": 0, "exact_fields": 0, "rate": 0.0}
        for question_id in sorted({key[2] for key in critical_keys})
    }
    for key in keys:
        verdicts = [mapping[key] for mapping in maps]
        values = [verdict.get("result") for verdict in verdicts]
        is_exact = all(value in VERDICTS for value in values) and len(set(values)) == 1
        if key in critical_key_set:
            exact += int(is_exact)
            row = critical_by_question[key[2]]
            row["fields"] = int(row["fields"]) + 1
            row["exact_fields"] = int(row["exact_fields"]) + int(is_exact)
        if not is_exact:
            path = atom_path(key)
            disagreements.append(path)
            if key in critical_key_set:
                critical_disagreements.append(path)
            _set_verdict(merged, key, unknown_from_disagreement(verdicts))
    for row in critical_by_question.values():
        fields = int(row["fields"])
        row["rate"] = int(row["exact_fields"]) / fields if fields else 0.0
    return merged, {
        "merge_version": MERGE_VERSION,
        "merge_status": MERGE_STATUS,
        "critical_atom_fields": len(critical_keys),
        "critical_atom_exact_fields": exact,
        "critical_atom_rate": exact / len(critical_keys),
        "disagreement_paths": disagreements,
        "critical_disagreement_paths": critical_disagreements,
        "critical_by_question": critical_by_question,
    }
