from pathlib import Path
from scripts.v2_core_stage_b1a2_active_link_compare_v2 import compare
ROOT=Path(__file__).resolve().parents[1];A=ROOT/"reports"/"course_backtest"/"2026-09-10"/"v2_core_reproducible_goal_v1"
def test_r3_captures_known_long_lineage_drift_without_upgrading_it():
    r=compare(artifact_dir=A,manifest_path=A/"stage_b1a2_active_link_validation_r2a_execution_manifest_r1.json")
    assert r["r3_metrics"]["raw_drift_candidate_count"]==1
    assert r["r3_metrics"]["drift_capture_percent"]==100
    case=next(x for x in r["r3_metrics"]["cases"] if x["review_id"]=="FP-b13c903946105e6b32eaf8d5")
    assert "SEG-8eaf427e149615725492" in case["final_unknown_ids"]
    assert "SEG-8eaf427e149615725492" not in case["final_shortlist_ids"]
    assert r["status"]=="SMOKE_PASSED_WITH_R3_ESCALATION（R3升級政策通過）"
