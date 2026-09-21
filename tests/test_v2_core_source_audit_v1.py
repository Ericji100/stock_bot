from __future__ import annotations

from pathlib import Path

import pytest

from scripts.v2_core_source_audit_v1 import BATCHES, audit_batch, classify


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {
                "structural_errors": 0,
                "future_outcome_rows": 0,
                "temporal_holdout_rows": 10,
                "row_count": 10,
                "complete_audit_rows": 10,
            },
            "VALID（有效）",
        ),
        (
            {
                "structural_errors": 0,
                "future_outcome_rows": 0,
                "temporal_holdout_rows": 0,
                "row_count": 10,
                "complete_audit_rows": 10,
            },
            "PARTIAL（部分有效）",
        ),
        (
            {
                "structural_errors": 1,
                "future_outcome_rows": 0,
                "temporal_holdout_rows": 10,
                "row_count": 10,
                "complete_audit_rows": 10,
            },
            "INVALID（無效）",
        ),
    ],
)
def test_classify(kwargs: dict[str, int], expected: str) -> None:
    assert classify(**kwargs) == expected


@pytest.mark.parametrize(
    ("batch_id", "expected_classification", "expected_complete"),
    [
        ("747", "PARTIAL（部分有效）", 0),
        ("889", "PARTIAL（部分有效）", 749),
        ("1029", "VALID（有效）", 1029),
    ],
)
def test_authoritative_batch_audit(
    batch_id: str, expected_classification: str, expected_complete: int
) -> None:
    result = audit_batch(ROOT, batch_id, BATCHES[batch_id])

    assert result["classification"] == expected_classification
    assert result["expected_rows"] == int(batch_id)
    assert result["actual_rows"] == int(batch_id)
    assert result["complete_audit_rows"] == expected_complete
    assert result["structural_errors"] == []
    assert result["packet_missing"] == 0
    assert result["packet_hash_mismatch"] == 0
    assert all(shard["matches"] for shard in result["shards"])


def test_v2_scenario_counts_are_reproduced() -> None:
    expected = {
        "747": {
            "BEAR_REVERSAL_LEFT_RIGHT": 5,
            "FRESH_Q1_EXPANSION": 55,
            "MACRO_COPY_RESONANCE": 66,
            "MATURE_TREND_PULLBACK": 64,
        },
        "889": {
            "BEAR_REVERSAL_LEFT_RIGHT": 1,
            "FRESH_Q1_EXPANSION": 15,
            "MACRO_COPY_RESONANCE": 100,
            "MATURE_TREND_PULLBACK": 80,
        },
        "1029": {
            "BEAR_REVERSAL_LEFT_RIGHT": 2,
            "FRESH_Q1_EXPANSION": 13,
            "MACRO_COPY_RESONANCE": 38,
            "MATURE_TREND_PULLBACK": 34,
        },
    }

    for batch_id, counts in expected.items():
        result = audit_batch(ROOT, batch_id, BATCHES[batch_id])
        assert result["scenario_counts"]["v2"] == counts
