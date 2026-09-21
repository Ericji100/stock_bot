"""Freeze the third fresh R4 upstream validation execution manifest."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from scripts.v2_core_objective_candidate_catalog_v1 import ARTIFACT_DIR,canonical_bytes,sha256_path,write_new_or_identical
from scripts.v2_core_stage_b1a1e_primary_gate_validation2_input_v1 import generate_all
OUTPUT_FILE="stage_b1a1e_primary_gate_validation2_execution_manifest_r1.json"
DESIGN_FILE="B1A1E_PRIMARY_GATE_FRESH_VALIDATION_R2.md"
def generate(artifact_dir:Path=ARTIFACT_DIR)->dict:
    source=generate_all(artifact_dir)
    bindings={"input_manifest_file":"stage_b1a1e_primary_gate_validation2_input_manifest_r1.json","prompt_file":"v2_core_stage_b1a1e_primary_gate.prompt.candidate_r4.md","schema_file":"v2_core_stage_b1a1e_primary_gate.schema.candidate_r4.json","runner_file":"scripts/v2_core_stage_b1a1e_primary_gate_runner_v1.py","validator_file":"scripts/v2_core_stage_b1a1e_primary_gate_validator_v1.py","comparator_file":"scripts/v2_core_stage_b1a1e_primary_gate_compare_v1.py","gate_file":DESIGN_FILE,"operational_policy_file":"operational_budget_override_v12.json"}
    manifest={"manifest_version":"v2-core-stage-b1a1e-primary-gate-fresh-validation-r2","status":"READY_FOR_FORMAL_AI_HELDOUT_VALIDATION（可執行正式AI留出驗證）","formal_model":"gpt-5.6-sol","reasoning_effort":"xhigh","case_count":source["case_count"],"required_rounds":3,"expected_case_rounds":source["case_count"]*3,"stop_remaining_percent_lte":3,"run_directory":"stage_b1a1e_primary_gate_validation2_runs_r1","input_packet_directory":source["input_packet_directory"],"program_gate_directory":source["program_gate_directory"],"program_gate_hidden_from_ai":True,"candidate_scales":["LARGE","SMALL"],"auxiliary_standalone_candidate_forbidden":True,"prior_review_ids_excluded":True,"prior_answers_present_in_ai_packets":False,"future_performance_used":False,"identity_used":False,"sealed_labels_used":False,**bindings,**{k.replace("_file","_sha256"):sha256_path(ROOT/v if v.startswith("scripts/") else artifact_dir/v) for k,v in bindings.items()},"rows":[{"review_id":x["review_id"],"as_of":x["as_of"],"input_packet_file":x["input_packet_file"],"input_packet_sha256":x["input_packet_sha256"],"program_gate_file":x["program_gate_file"],"program_gate_sha256":x["program_gate_sha256"],"selected_candidate_count":x["selected_candidate_count"],"directional_evidence_option_count":x["directional_evidence_option_count"],"objective_contact_candidate_count":x["objective_contact_candidate_count"]} for x in source["rows"]]}
    write_new_or_identical(artifact_dir/OUTPUT_FILE,canonical_bytes(manifest));return manifest
def main()->int:
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--artifact-dir",type=Path,default=ARTIFACT_DIR);args=parser.parse_args();print(json.dumps(generate(args.artifact_dir),ensure_ascii=False,indent=2));return 0
if __name__=="__main__":raise SystemExit(main())
