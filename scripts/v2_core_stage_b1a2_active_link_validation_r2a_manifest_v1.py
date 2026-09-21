"""Freeze truly fresh B1a2 R2A held-out execution manifest."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from scripts.v2_core_objective_candidate_catalog_v1 import ARTIFACT_DIR,canonical_bytes,sha256_path,write_new_or_identical
from scripts.v2_core_stage_b1a2_active_link_validation_r2a_input_v1 import generate_all
OUTPUT_FILE="stage_b1a2_active_link_validation_r2a_execution_manifest_r1.json"
def generate(artifact_dir:Path=ARTIFACT_DIR)->dict:
    source=generate_all(artifact_dir)
    files={"input_manifest_sha256":artifact_dir/"stage_b1a2_active_link_validation_r2a_input_manifest_r1.json","prompt_sha256":artifact_dir/"v2_core_stage_b1a2_active_link.prompt.candidate_r2.md","schema_sha256":artifact_dir/"v2_core_stage_b1a2_active_link.schema.candidate_r2a.json","design_sha256":artifact_dir/"B1A2_ACTIVE_CAMPAIGN_LINK_HELDOUT_VALIDATION_R2A.md","policy_sha256":artifact_dir/"operational_budget_override_v12.json","runner_sha256":ROOT/"scripts/v2_core_stage_b1a2_active_link_runner_v1.py","validator_sha256":ROOT/"scripts/v2_core_stage_b1a2_active_link_validator_v1.py","comparator_sha256":ROOT/"scripts/v2_core_stage_b1a2_active_link_compare_v1.py"};bindings={k:sha256_path(v) for k,v in files.items()}
    m={"manifest_version":"v2-core-stage-b1a2-active-link-heldout-r2a-execution-r1","status":"READY_FOR_FORMAL_AI_HELDOUT_VALIDATION（可執行正式AI留出驗證）","formal_model":"gpt-5.6-sol","reasoning_effort":"xhigh","case_count":source["case_count"],"required_rounds":3,"expected_case_rounds":source["case_count"]*3,"stop_remaining_percent_lte":3.0,"input_manifest_file":"stage_b1a2_active_link_validation_r2a_input_manifest_r1.json","input_packet_directory":source["input_packet_directory"],"prompt_file":"v2_core_stage_b1a2_active_link.prompt.candidate_r2.md","schema_file":"v2_core_stage_b1a2_active_link.schema.candidate_r2a.json","design_file":"B1A2_ACTIVE_CAMPAIGN_LINK_HELDOUT_VALIDATION_R2A.md","policy_file":"operational_budget_override_v12.json","runner_file":"scripts/v2_core_stage_b1a2_active_link_runner_v1.py","validator_file":"scripts/v2_core_stage_b1a2_active_link_validator_v1.py","comparator_file":"scripts/v2_core_stage_b1a2_active_link_compare_v1.py","run_directory":"stage_b1a2_active_link_validation_r2a_runs_r1","future_performance_used":False,"identity_used":False,"sealed_labels_used":False,"prior_role_answers_used":False,"r1_or_r2a_answers_used":False,"course_role_generated_by_program":False,"bindings":bindings,"rows":source["rows"]}
    write_new_or_identical(artifact_dir/OUTPUT_FILE,canonical_bytes(m));return m
def main()->int:
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--artifact-dir",type=Path,default=ARTIFACT_DIR);args=parser.parse_args();print(json.dumps(generate(args.artifact_dir),ensure_ascii=False,indent=2));return 0
if __name__=="__main__":raise SystemExit(main())
