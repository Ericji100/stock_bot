"""Build truly fresh B1a2 R2A held-out inputs from the third R4 batch."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
from typing import Any
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from scripts.v2_core_objective_candidate_catalog_v1 import ARTIFACT_DIR,canonical_bytes,sha256_path,write_new_or_identical
from scripts.v2_core_stage_b1a1e_primary_gate_validator_v1 import load_json
from scripts.v2_core_stage_b1a2_active_link_input_v1 import SOURCE_B1A1_DIRECTORY,SOURCE_B1A1_MANIFEST,_r4_validations
from scripts.v2_core_stage_b1a2_active_link_input_v2 import build_packet
SOURCE_EXECUTION_MANIFEST="stage_b1a1e_primary_gate_validation2_execution_manifest_r1.json"
SOURCE_FINAL_REPORT="stage_b1a1e_primary_gate_validation2_report_r1.json"
OUTPUT_DIRECTORY="stage_b1a2_active_link_validation_r2a_input_packets_r1"
OUTPUT_MANIFEST="stage_b1a2_active_link_validation_r2a_input_manifest_r1.json"
def generate_all(artifact_dir:Path=ARTIFACT_DIR)->dict[str,Any]:
    execution=load_json(artifact_dir/SOURCE_EXECUTION_MANIFEST);report=load_json(artifact_dir/SOURCE_FINAL_REPORT)
    if report.get("status")!="SMOKE_PASSED（Smoke通過）":raise ValueError("fresh upstream R4 batch not passed")
    b1=load_json(artifact_dir/SOURCE_B1A1_MANIFEST);b1rows={str(x["review_id"]):x for x in b1["rows"]};outdir=artifact_dir/OUTPUT_DIRECTORY;outdir.mkdir(parents=True,exist_ok=True)
    rows=[];tc=tp=0
    for row in execution["rows"]:
        rid=str(row["review_id"]);vals=_r4_validations(artifact_dir,execution,row);eligible=list(vals[0]["partial_eligible_ids"]);sr=b1rows[rid];sp=artifact_dir/SOURCE_B1A1_DIRECTORY/sr["input_packet_file"]
        if sha256_path(sp)!=sr["input_packet_sha256"]:raise ValueError(f"B1a1 hash mismatch: {rid}")
        packet=build_packet(load_json(sp),rid,str(row["as_of"]),eligible,sha256_path(artifact_dir/SOURCE_FINAL_REPORT));packet["packet_version"]="v2-core-stage-b1a2-active-link-heldout-r2a-input-r1";packet["status"]="HELDOUT_INPUT_NOT_FORMALLY_RUN（留出輸入、尚未正式執行）"
        op=outdir/f"{rid}.json";write_new_or_identical(op,canonical_bytes(packet));pc=sum(len(x["fixed_relation_paths"]) for x in packet["role_link_evidence_options"]);tc+=len(eligible);tp+=pc
        rows.append({"review_id":rid,"as_of":row["as_of"],"input_packet_file":op.name,"input_packet_sha256":sha256_path(op),"candidate_count":len(eligible),"fixed_path_count":pc,"source_b1a1_packet_sha256":sha256_path(sp)})
    m={"manifest_version":"v2-core-stage-b1a2-active-link-heldout-r2a-input-r1","status":"FRESH_HELDOUT_INPUTS_COMPLETE_NOT_READY_FOR_AI（全新留出輸入完成、尚不可執行AI）","case_count":len(rows),"candidate_count":tc,"fixed_path_count":tp,"formal_ai_calls":0,"ready_for_formal_ai":False,"formal_model":"gpt-5.6-sol","reasoning_effort":"xhigh","source_r4_execution_manifest_file":SOURCE_EXECUTION_MANIFEST,"source_r4_execution_manifest_sha256":sha256_path(artifact_dir/SOURCE_EXECUTION_MANIFEST),"source_r4_final_report_file":SOURCE_FINAL_REPORT,"source_r4_final_report_sha256":sha256_path(artifact_dir/SOURCE_FINAL_REPORT),"input_packet_directory":OUTPUT_DIRECTORY,"future_performance_used":False,"identity_used":False,"sealed_labels_used":False,"prior_role_answers_used":False,"r1_or_r2a_answers_used":False,"course_role_generated_by_program":False,"rows":rows}
    write_new_or_identical(artifact_dir/OUTPUT_MANIFEST,canonical_bytes(m));return m
def main()->int:
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--artifact-dir",type=Path,default=ARTIFACT_DIR);args=parser.parse_args();print(json.dumps(generate_all(args.artifact_dir),ensure_ascii=False,indent=2));return 0
if __name__=="__main__":raise SystemExit(main())
