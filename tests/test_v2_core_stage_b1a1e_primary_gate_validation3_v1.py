from pathlib import Path
from scripts.v2_core_stage_b1a1e_primary_gate_validation3_input_v1 import generate_all
from scripts.v2_core_stage_b1a1e_primary_gate_validator_v1 import load_json
ROOT=Path(__file__).resolve().parents[1];A=ROOT/"reports"/"course_backtest"/"2026-09-10"/"v2_core_reproducible_goal_v1"
def test_fourth_batch_excludes_third_batch():
 m=generate_all(A);old=load_json(A/"stage_b1a1e_primary_gate_validation2_execution_manifest_r1.json");assert len(m["rows"])==4 and {x["review_id"] for x in m["rows"]}.isdisjoint({x["review_id"] for x in old["rows"]})
