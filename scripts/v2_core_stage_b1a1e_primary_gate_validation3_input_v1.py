"""Build a fourth fresh R4 case set for B1a2 R3 validation."""
from pathlib import Path
from scripts.v2_core_objective_candidate_catalog_v1 import ARTIFACT_DIR
from scripts import v2_core_stage_b1a1e_primary_gate_validation2_input_v1 as base

def generate_all(artifact_dir:Path=ARTIFACT_DIR):
    original=(base.SELECTION_SEED,base.EXCLUSION_MANIFESTS,base.OUTPUT_DIRECTORY,base.PROGRAM_GATE_DIRECTORY,base.OUTPUT_MANIFEST,base.OUTPUT_SELECTION)
    try:
        base.SELECTION_SEED="B1A1E-R4-PRIMARY-OBJECTIVE-GATE-FRESH-VALIDATION-R3"
        base.EXCLUSION_MANIFESTS=base.EXCLUSION_MANIFESTS+("stage_b1a1e_primary_gate_validation2_execution_manifest_r1.json",)
        base.OUTPUT_DIRECTORY="stage_b1a1e_primary_gate_validation3_input_packets_r1"
        base.PROGRAM_GATE_DIRECTORY="stage_b1a1e_primary_gate_validation3_program_inputs_r1"
        base.OUTPUT_MANIFEST="stage_b1a1e_primary_gate_validation3_input_manifest_r1.json"
        base.OUTPUT_SELECTION="stage_b1a1e_primary_gate_validation3_selection_r1.json"
        return base.generate_all(artifact_dir)
    finally:
        (base.SELECTION_SEED,base.EXCLUSION_MANIFESTS,base.OUTPUT_DIRECTORY,base.PROGRAM_GATE_DIRECTORY,base.OUTPUT_MANIFEST,base.OUTPUT_SELECTION)=original
if __name__=="__main__":
    import argparse,json
    p=argparse.ArgumentParser();p.add_argument("--artifact-dir",type=Path,default=ARTIFACT_DIR);a=p.parse_args();print(json.dumps(generate_all(a.artifact_dir),ensure_ascii=False,indent=2))
