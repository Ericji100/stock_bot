from pathlib import Path

from scripts.v2_core_stage_b1a2_active_link_compare_v3 import compare


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"


def test_two_edge_policy_captures_r3_calibration_drift():
    manifest_path = ARTIFACT_DIR / "stage_b1a2_active_link_validation_r3_execution_manifest_r1.json"
    manifest = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))
    manifest["escalation_min_fixed_path_edges"] = 2
    temporary = ARTIFACT_DIR / "stage_b1a2_active_link_validation_r3_execution_manifest_r1.threshold2.test.json"
    temporary.write_text(__import__("json").dumps(manifest, ensure_ascii=False), encoding="utf-8")
    try:
        report = compare(artifact_dir=ARTIFACT_DIR, manifest_path=temporary)
    finally:
        temporary.unlink(missing_ok=True)
    assert report["escalation_metrics"]["raw_drift_candidate_count"] == 1
    assert report["escalation_metrics"]["drift_capture_percent"] == 100.0
    assert report["escalation_metrics"]["final_shortlist_reproducibility_percent"] == 100.0
