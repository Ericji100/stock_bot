"""Freeze transport-compatible B1a2 active-link R2A execution manifest."""

from __future__ import annotations
import argparse, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from scripts.v2_core_objective_candidate_catalog_v1 import ARTIFACT_DIR, canonical_bytes, sha256_path, write_new_or_identical
from scripts.v2_core_stage_b1a2_active_link_input_v2 import generate_all

OUTPUT_FILE="stage_b1a2_active_link_execution_manifest_candidate_r2a.json"
INPUT_MANIFEST="stage_b1a2_active_link_input_manifest_candidate_r2.json"
PROMPT_FILE="v2_core_stage_b1a2_active_link.prompt.candidate_r2.md"
SCHEMA_FILE="v2_core_stage_b1a2_active_link.schema.candidate_r2a.json"
DESIGN_FILE="B1A2_ACTIVE_CAMPAIGN_LINK_CANDIDATE_R2A.md"
POLICY_FILE="operational_budget_override_v12.json"
RUNNER_FILE="scripts/v2_core_stage_b1a2_active_link_runner_v1.py"
VALIDATOR_FILE="scripts/v2_core_stage_b1a2_active_link_validator_v1.py"
COMPARATOR_FILE="scripts/v2_core_stage_b1a2_active_link_compare_v1.py"
RUN_DIRECTORY="stage_b1a2_active_link_runs_candidate_r2a"

def generate(artifact_dir:Path=ARTIFACT_DIR)->dict:
    source=generate_all(artifact_dir)
    files={"input_manifest_sha256":artifact_dir/INPUT_MANIFEST,"prompt_sha256":artifact_dir/PROMPT_FILE,"schema_sha256":artifact_dir/SCHEMA_FILE,"design_sha256":artifact_dir/DESIGN_FILE,"policy_sha256":artifact_dir/POLICY_FILE,"runner_sha256":ROOT/RUNNER_FILE,"validator_sha256":ROOT/VALIDATOR_FILE,"comparator_sha256":ROOT/COMPARATOR_FILE}
    bindings={k:sha256_path(v) for k,v in files.items()}
    manifest={"manifest_version":"v2-core-stage-b1a2-active-link-execution-r2a-candidate","status":"READY_FOR_FORMAL_SMOKE（可執行正式Smoke）","formal_model":"gpt-5.6-sol","reasoning_effort":"xhigh","case_count":source["case_count"],"required_rounds":3,"expected_case_rounds":source["case_count"]*3,"stop_remaining_percent_lte":3.0,"input_manifest_file":INPUT_MANIFEST,"input_packet_directory":source["input_packet_directory"],"prompt_file":PROMPT_FILE,"schema_file":SCHEMA_FILE,"design_file":DESIGN_FILE,"policy_file":POLICY_FILE,"runner_file":RUNNER_FILE,"validator_file":VALIDATOR_FILE,"comparator_file":COMPARATOR_FILE,"run_directory":RUN_DIRECTORY,"future_performance_used":False,"identity_used":False,"sealed_labels_used":False,"prior_role_answers_used":False,"course_role_generated_by_program":False,"technical_delta_from_r2":"ADD_TRANSPORT_DEFS_ONLY","bindings":bindings,"rows":source["rows"]}
    write_new_or_identical(artifact_dir/OUTPUT_FILE,canonical_bytes(manifest)); return manifest

def main()->int:
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--artifact-dir",type=Path,default=ARTIFACT_DIR); args=parser.parse_args(); print(json.dumps(generate(args.artifact_dir),ensure_ascii=False,indent=2)); return 0
if __name__=="__main__": raise SystemExit(main())
