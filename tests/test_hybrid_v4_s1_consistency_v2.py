from __future__ import annotations

import copy

import pytest

from scripts import hybrid_v4_s1_consistency_v1 as v1
from scripts import hybrid_v4_s1_consistency_v2 as v2


def _empty_output(review_id: str = "D-empty") -> dict:
    return {
        "review_id": review_id,
        "global_answers": {},
        "candidate_answers": {
            "anchor_candidates": [],
            "relation_candidates": [],
            "stop_candidates": [],
        },
    }


def _one_atom(result: str = "PASS") -> dict:
    output = _empty_output("D-one")
    output["global_answers"] = {
        "LARGE_NEXT_UP_DIRECTION_SUPPORTED": {
            "result": result,
            "supporting_evidence_refs": ["E-1"],
            "contradicting_evidence_refs": [],
            "missing_evidence_codes": [],
            "reason_code": "VISIBLE_SUPPORT",
        }
    }
    return output


def test_three_empty_manifests_merge_without_fabricating_atoms() -> None:
    outputs = [_empty_output() for _ in range(3)]
    merged, metrics = v2.merge_empty_manifest_aware(
        outputs,
        critical_ids={"LARGE_NEXT_UP_DIRECTION_SUPPORTED"},
    )

    assert merged == outputs[0]
    assert merged is not outputs[0]
    assert metrics["critical_atom_fields"] == 0
    assert metrics["critical_atom_rate"] is None
    assert metrics["disagreement_paths"] == []
    assert metrics["empty_manifest_handling"] == v2.AMENDMENT_CODE


def test_non_empty_manifest_is_identical_to_frozen_merge() -> None:
    outputs = [_one_atom() for _ in range(3)]
    expected = v1.conservative_merge_atomic_outputs(
        copy.deepcopy(outputs),
        critical_ids={"LARGE_NEXT_UP_DIRECTION_SUPPORTED"},
    )
    actual = v2.merge_empty_manifest_aware(
        copy.deepcopy(outputs),
        critical_ids={"LARGE_NEXT_UP_DIRECTION_SUPPORTED"},
    )
    assert actual == expected


def test_evaluate_restores_frozen_v1_merge_after_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    original = v1.conservative_merge_atomic_outputs

    def fail(**_kwargs):
        assert v1.conservative_merge_atomic_outputs is v2.merge_empty_manifest_aware
        raise RuntimeError("expected")

    monkeypatch.setattr(v1, "evaluate", fail)
    with pytest.raises(RuntimeError, match="expected"):
        v2.evaluate()
    assert v1.conservative_merge_atomic_outputs is original
