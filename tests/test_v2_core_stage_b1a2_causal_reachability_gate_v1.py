from pathlib import Path

from scripts.v2_core_objective_candidate_catalog_v1 import canonical_bytes
from scripts.v2_core_stage_b1a2_causal_reachability_gate_v1 import evaluate_manifests


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
MANIFESTS = [
    ARTIFACT_DIR / "stage_b1a2_active_link_execution_manifest_candidate_r2a.json",
    ARTIFACT_DIR / "stage_b1a2_active_link_validation_r2a_execution_manifest_r1.json",
    ARTIFACT_DIR / "stage_b1a2_active_link_validation_r3_execution_manifest_r1.json",
    ARTIFACT_DIR / "stage_b1a2_active_link_validation_r4_execution_manifest_r1.json",
]


def test_r5_gate_is_deterministic_and_assigns_no_course_role():
    first = evaluate_manifests(artifact_dir=ARTIFACT_DIR, manifest_paths=MANIFESTS)
    second = evaluate_manifests(artifact_dir=ARTIFACT_DIR, manifest_paths=MANIFESTS)
    assert canonical_bytes(first) == canonical_bytes(second)
    assert first["status"] == "PASS（通過）"
    assert first["candidate_count"] == 63
    assert first["contract_error_count"] == 0
    assert first["course_role_assigned"] is False
    assert first["trade_permission_granted"] is False
    assert first["future_performance_used"] is False
    assert first["prior_ai_active_link_answers_used"] is False


def test_r5_preserves_direct_and_longer_causal_paths_for_role_ai():
    report = evaluate_manifests(artifact_dir=ARTIFACT_DIR, manifest_paths=MANIFESTS)
    rows = {
        (case["review_id"], row["candidate_id"]): row
        for case in report["cases"]
        for row in case["candidate_results"]
    }
    assert rows[("FP-ffc9a53dff87b7e2eac44b39", "SEG-a06076e0237e8beb3835")]["min_fixed_path_edges"] == 1
    assert rows[("FP-ffc9a53dff87b7e2eac44b39", "SEG-a06076e0237e8beb3835")]["r5_gate"] == "RETAIN_FOR_ROLE_AI"
    assert rows[("FP-5d130963e17ef8b413cda989", "SEG-e0284ea367d80d1b5321")]["min_fixed_path_edges"] == 2
    assert rows[("FP-5d130963e17ef8b413cda989", "SEG-e0284ea367d80d1b5321")]["r5_gate"] == "RETAIN_FOR_ROLE_AI"
