from __future__ import annotations

import json
from pathlib import Path

from scripts.v2_core_legacy_teacher_pairwise_audit_v1 import build_audit


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
)


def test_pairwise_audit_is_calibration_only_and_blind_to_outcomes() -> None:
    audit = build_audit(ARTIFACT_DIR)
    assert audit["case_count"] == 14
    assert audit["formal_ai_calls"] == 0
    assert audit["future_performance_used"] is False
    assert audit["locked_reproduction_set_opened"] is False
    assert audit["identity_used"] is False
    assert audit["teacher_answers_allowed_in_formal_ai_input"] is False


def test_pairwise_audit_excludes_teacher_equivalents_from_neighbours() -> None:
    audit = build_audit(ARTIFACT_DIR)
    for case in audit["cases"]:
        teacher_ids = {
            candidate["candidate_id"]
            for candidate in case["teacher_equivalent_candidates"]
        }
        neighbours = case["nearest_non_teacher_candidates"]
        assert len(neighbours) == 3
        assert [row["rank"] for row in neighbours] == [1, 2, 3]
        assert teacher_ids.isdisjoint(
            row["candidate"]["candidate_id"] for row in neighbours
        )
        assert [row["distance"] for row in neighbours] == sorted(
            row["distance"] for row in neighbours
        )


def test_pairwise_audit_reproduces_frozen_r4_mismatch_counts() -> None:
    audit = build_audit(ARTIFACT_DIR)
    summary = audit["summary"]
    assert summary["r4_teacher_equivalent_count"] == 1
    assert summary["r4_mismatch_count"] == 13
    assert sum(
        row["case_count"] for row in summary["by_teacher_scenario"].values()
    ) == 14


def test_pairwise_audit_serialization_has_no_outcome_fields() -> None:
    audit = build_audit(ARTIFACT_DIR)
    serialized = json.dumps(audit, ensure_ascii=False).lower()
    for forbidden in ('"mfe"', '"mae"', '"return_pct"', '"future_bars"', '"winner"'):
        assert forbidden not in serialized
