import pytest

from scripts.hybrid_v3_conservative_merge_v1 import (
    ConservativeMergeError,
    unknown_from_disagreement,
)


def _verdict(result: str, support: list[str], contradict: list[str]) -> dict:
    return {
        "result": result,
        "supporting_evidence_refs": support,
        "contradicting_evidence_refs": contradict,
        "missing_evidence_codes": [] if result != "UNKNOWN" else ["OTHER_REQUIRED_EVIDENCE"],
        "reason_code": "VISIBLE_EVIDENCE_SATISFIES_DEFINITION",
    }


def test_disagreement_evidence_is_disjoint_and_complete() -> None:
    result = unknown_from_disagreement(
        [
            _verdict("PASS", ["E:1", "E:2"], []),
            _verdict("FAIL", [], ["E:1", "E:3"]),
            _verdict("UNKNOWN", ["E:4"], ["E:2"]),
        ]
    )
    assert result["result"] == "UNKNOWN"
    assert result["supporting_evidence_refs"] == ["E:1", "E:2", "E:4"]
    assert result["contradicting_evidence_refs"] == ["E:3"]
    assert not (
        set(result["supporting_evidence_refs"])
        & set(result["contradicting_evidence_refs"])
    )
    assert result["missing_evidence_codes"] == ["CONFLICTING_VISIBLE_EVIDENCE"]


def test_disagreement_without_visible_ref_is_rejected() -> None:
    with pytest.raises(ConservativeMergeError, match="cannot fabricate"):
        unknown_from_disagreement([None, None, None])
