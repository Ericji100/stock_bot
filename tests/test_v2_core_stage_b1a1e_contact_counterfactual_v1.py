from pathlib import Path

from scripts.v2_core_stage_b1a1e_contact_counterfactual_v1 import analyze


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT
    / "reports"
    / "course_backtest"
    / "2026-09-10"
    / "v2_core_reproducible_goal_v1"
)


def test_primary_r3_counterfactual_is_blind_and_complete() -> None:
    report = analyze(
        artifact_dir=ARTIFACT_DIR,
        manifest_path=ARTIFACT_DIR
        / "stage_b1a1e_primary_execution_manifest_candidate_r3.json",
    )
    assert report["case_count"] == 4
    assert report["candidate_count"] == 24
    assert report["required_rounds"] == 3
    assert report["counterfactual_contract"] == {
        "ai_decides_meaningful_same_level_target": True,
        "program_decides_objective_target_contact": True,
        "saved_ai_outputs_modified": False,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
    }


def test_primary_r3_contact_counterfactual_keeps_every_eligible_set_stable() -> None:
    report = analyze(
        artifact_dir=ARTIFACT_DIR,
        manifest_path=ARTIFACT_DIR
        / "stage_b1a1e_primary_execution_manifest_candidate_r3.json",
    )
    # One same-level perception still drifts on a candidate whose direction is
    # consistently false.  It cannot change eligibility, and must remain
    # visible in the diagnostic rather than being erased.
    assert sum(len(row["drift_candidates"]) for row in report["cases"]) == 1
    assert report["candidate_eligibility_consistency_percent"] == 100.0
    assert report["partial_eligible_set_consistency_percent"] == 100.0
