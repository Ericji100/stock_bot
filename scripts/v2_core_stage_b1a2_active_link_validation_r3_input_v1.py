"""Build fresh B1a2 R3 validation inputs from the fourth R4 batch."""
from pathlib import Path
from scripts.v2_core_objective_candidate_catalog_v1 import ARTIFACT_DIR
from scripts import v2_core_stage_b1a2_active_link_validation_r2a_input_v1 as base
def generate_all(artifact_dir:Path=ARTIFACT_DIR):
 original=(base.SOURCE_EXECUTION_MANIFEST,base.SOURCE_FINAL_REPORT,base.OUTPUT_DIRECTORY,base.OUTPUT_MANIFEST)
 try:
  base.SOURCE_EXECUTION_MANIFEST="stage_b1a1e_primary_gate_validation3_execution_manifest_r1.json";base.SOURCE_FINAL_REPORT="stage_b1a1e_primary_gate_validation3_report_r1.json";base.OUTPUT_DIRECTORY="stage_b1a2_active_link_validation_r3_input_packets_r1";base.OUTPUT_MANIFEST="stage_b1a2_active_link_validation_r3_input_manifest_r1.json";return base.generate_all(artifact_dir)
 finally:
  base.SOURCE_EXECUTION_MANIFEST,base.SOURCE_FINAL_REPORT,base.OUTPUT_DIRECTORY,base.OUTPUT_MANIFEST=original
if __name__=="__main__":
 import argparse,json;p=argparse.ArgumentParser();p.add_argument("--artifact-dir",type=Path,default=ARTIFACT_DIR);a=p.parse_args();print(json.dumps(generate_all(a.artifact_dir),ensure_ascii=False,indent=2))
