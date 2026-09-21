from pathlib import Path
from scripts.v2_core_stage_b1a2_active_link_validation_r3_manifest_v1 import generate
from scripts import v2_core_stage_b1a2_active_link_validation_r2a_input_v1 as r2a_base
ROOT=Path(__file__).resolve().parents[1];A=ROOT/"reports"/"course_backtest"/"2026-09-10"/"v2_core_reproducible_goal_v1"
def test_r3_manifest_freezes_escalation_before_outputs():
 before=(r2a_base.SOURCE_EXECUTION_MANIFEST,r2a_base.SOURCE_FINAL_REPORT,r2a_base.OUTPUT_DIRECTORY,r2a_base.OUTPUT_MANIFEST)
 m=generate(A)
 after=(r2a_base.SOURCE_EXECUTION_MANIFEST,r2a_base.SOURCE_FINAL_REPORT,r2a_base.OUTPUT_DIRECTORY,r2a_base.OUTPUT_MANIFEST)
 assert m["case_count"]==4 and m["expected_case_rounds"]==12 and m["escalation_min_fixed_path_edges"]==3 and m["escalation_requires_unanimous_support"] is True
 assert after==before
