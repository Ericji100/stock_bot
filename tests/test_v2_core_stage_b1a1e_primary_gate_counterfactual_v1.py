from pathlib import Path

import pytest

from scripts.v2_core_stage_b1a1e_primary_gate_counterfactual_v1 import analyze


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT
    / "reports"
    / "course_backtest"
    / "2026-09-10"
    / "v2_core_reproducible_goal_v1"
)


@pytest.mark.parametrize(
    "manifest_name,case_count,candidate_count",
    [
        ("stage_b1a1e_smoke_execution_manifest_candidate_r2.json", 4, 22),
        ("stage_b1a1e_validation_execution_manifest_r1.json", 4, 24),
        ("stage_b1a1e_primary_execution_manifest_candidate_r3.json", 4, 24),
    ],
)
def test_primary_gate_is_stable_across_all_existing_three_round_sets(
    manifest_name: str, case_count: int, candidate_count: int
) -> None:
    report = analyze(
        artifact_dir=ARTIFACT_DIR,
        manifest_path=ARTIFACT_DIR / manifest_name,
    )
    assert report["case_count"] == case_count
    assert report["candidate_count"] == candidate_count
    assert report["directional_atom_consistency_percent"] == 100.0
    assert report["candidate_eligibility_consistency_percent"] == 100.0
    assert report["partial_eligible_set_consistency_percent"] == 100.0
    assert all(not row["drift_candidates"] for row in report["cases"])


def test_primary_gate_is_only_a_candidate_pool_gate() -> None:
    report = analyze(
        artifact_dir=ARTIFACT_DIR,
        manifest_path=ARTIFACT_DIR
        / "stage_b1a1e_primary_execution_manifest_candidate_r3.json",
    )
    contract = report["counterfactual_contract"]
    assert contract["controlling_anchor_or_course_role_decided"] is False
    assert contract["trade_permission_granted"] is False
    assert contract["saved_ai_outputs_modified"] is False
    assert contract["future_performance_used"] is False
