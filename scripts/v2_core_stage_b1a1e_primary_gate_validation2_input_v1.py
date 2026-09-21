"""Build a third fresh four-case set for frozen R4 upstream validation."""

from __future__ import annotations
import argparse, hashlib, json, sys
from pathlib import Path
from typing import Any
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from scripts.v2_core_objective_candidate_catalog_v1 import ARTIFACT_DIR, canonical_bytes, sha256_path, write_new_or_identical
from scripts.v2_core_stage_b1a1_evidence_options_v1 import generate_all as generate_options
from scripts.v2_core_stage_b1a1e_focus_input_v1 import SOURCE_OPTION_DIRECTORY, SOURCE_OPTION_MANIFEST, SOURCE_PACKET_DIRECTORY, load_json
from scripts.v2_core_stage_b1a1e_primary_gate_input_v1 import SOURCE_INPUT_MANIFEST, _build_packets, _select_candidates

SELECTION_SEED="B1A1E-R4-PRIMARY-OBJECTIVE-GATE-FRESH-VALIDATION-R2"
CASE_COUNT=4
EXCLUSION_MANIFESTS=(
 "stage_b1a1e_smoke_execution_manifest_candidate_r2.json",
 "stage_b1a1e_validation_execution_manifest_r1.json",
 "stage_b1a1e_primary_execution_manifest_candidate_r3.json",
 "stage_b1a1e_primary_gate_execution_manifest_candidate_r4.json",
 "stage_b1a1e_primary_gate_validation_execution_manifest_r1.json",
)
OUTPUT_DIRECTORY="stage_b1a1e_primary_gate_validation2_input_packets_r1"
PROGRAM_GATE_DIRECTORY="stage_b1a1e_primary_gate_validation2_program_inputs_r1"
OUTPUT_MANIFEST="stage_b1a1e_primary_gate_validation2_input_manifest_r1.json"
OUTPUT_SELECTION="stage_b1a1e_primary_gate_validation2_selection_r1.json"

def _digest(*parts:str)->str: return hashlib.sha256("|".join((SELECTION_SEED,*parts)).encode()).hexdigest()

def generate_all(artifact_dir:Path=ARTIFACT_DIR)->dict[str,Any]:
    generate_options(artifact_dir)
    source=load_json(artifact_dir/SOURCE_INPUT_MANIFEST); source_rows={str(x["review_id"]):x for x in source["rows"]}
    options=load_json(artifact_dir/SOURCE_OPTION_MANIFEST); option_rows={str(x["review_id"]):x for x in options["rows"]}
    excluded=set()
    for name in EXCLUSION_MANIFESTS: excluded.update(str(x["review_id"]) for x in load_json(artifact_dir/name)["rows"])
    eligible=[x for x in source_rows if x not in excluded]
    review_ids=sorted(eligible,key=lambda x:(_digest("CASE",x),x))[:CASE_COUNT]
    if len(review_ids)!=CASE_COUNT: raise ValueError("insufficient fresh cases")
    outdir=artifact_dir/OUTPUT_DIRECTORY; gatedir=artifact_dir/PROGRAM_GATE_DIRECTORY; outdir.mkdir(parents=True,exist_ok=True); gatedir.mkdir(parents=True,exist_ok=True)
    rows=[]; selections=[]; totals={"selected_candidate_count":0,"directional_evidence_option_count":0,"objective_contact_candidate_count":0}
    for rid in review_ids:
        sr,orr=source_rows[rid],option_rows[rid]
        source_path=artifact_dir/SOURCE_PACKET_DIRECTORY/sr["input_packet_file"]; option_path=artifact_dir/SOURCE_OPTION_DIRECTORY/orr["catalog_file"]
        if sha256_path(source_path)!=sr["input_packet_sha256"] or sha256_path(option_path)!=orr["catalog_sha256"]: raise ValueError(f"source hash mismatch: {rid}")
        chosen=_select_candidates(load_json(source_path)); ai_packet,program_gate=_build_packets(source_packet=load_json(source_path),option_catalog=load_json(option_path),chosen=chosen)
        packet_path=outdir/f"{rid}.json"; gate_path=gatedir/f"{rid}.json"; write_new_or_identical(packet_path,canonical_bytes(ai_packet)); write_new_or_identical(gate_path,canonical_bytes(program_gate))
        contacts=sum(x["objective_same_scale_contact"] for x in program_gate["candidate_rows"])
        totals["selected_candidate_count"]+=len(chosen); totals["directional_evidence_option_count"]+=len(ai_packet["evidence_options"]); totals["objective_contact_candidate_count"]+=contacts
        selections.append({"review_id":rid,"case_selection_digest":_digest("CASE",rid),"selected_candidates":[{"candidate_id":x["candidate_id"],"scale":x["scale"],"direction":x["direction"],"status":x["status"]} for x in chosen]})
        rows.append({"review_id":rid,"as_of":ai_packet["as_of"],"input_packet_file":packet_path.name,"input_packet_sha256":sha256_path(packet_path),"program_gate_file":gate_path.name,"program_gate_sha256":sha256_path(gate_path),"selected_candidate_count":len(chosen),"directional_evidence_option_count":len(ai_packet["evidence_options"]),"objective_contact_candidate_count":contacts,"source_packet_sha256":sha256_path(source_path),"source_option_catalog_sha256":sha256_path(option_path)})
    selection={"selection_version":"v2-core-stage-b1a1e-primary-gate-fresh-validation-r2","selection_seed":SELECTION_SEED,"excluded_prior_review_ids":sorted(excluded),"uses_prior_ai_answers":False,"uses_future_performance":False,"uses_identity":False,"uses_sealed_labels":False,"rows":selections}
    write_new_or_identical(artifact_dir/OUTPUT_SELECTION,canonical_bytes(selection))
    manifest={"manifest_version":"v2-core-stage-b1a1e-primary-gate-fresh-validation-input-r2","status":"FRESH_HELDOUT_INPUTS_COMPLETE_NOT_READY_FOR_AI（全新留出輸入完成、尚不可執行AI）","case_count":len(rows),**totals,"formal_ai_calls":0,"ready_for_formal_ai":False,"formal_model":"gpt-5.6-sol","reasoning_effort":"xhigh","candidate_scales":["LARGE","SMALL"],"program_gate_hidden_from_ai":True,"prior_review_ids_excluded":True,"prior_answers_present_in_ai_packets":False,"future_performance_used":False,"identity_used":False,"sealed_labels_used":False,"course_judgement_generated_by_program":False,"selection_file":OUTPUT_SELECTION,"selection_sha256":sha256_path(artifact_dir/OUTPUT_SELECTION),"input_packet_directory":OUTPUT_DIRECTORY,"program_gate_directory":PROGRAM_GATE_DIRECTORY,"rows":rows}
    write_new_or_identical(artifact_dir/OUTPUT_MANIFEST,canonical_bytes(manifest)); return manifest

def main()->int:
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--artifact-dir",type=Path,default=ARTIFACT_DIR);args=parser.parse_args();print(json.dumps(generate_all(args.artifact_dir),ensure_ascii=False,indent=2));return 0
if __name__=="__main__":raise SystemExit(main())
