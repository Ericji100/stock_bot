"""Build a fifth fresh R4 upstream case set for B1a2 R4 validation."""

from pathlib import Path

from scripts.v2_core_objective_candidate_catalog_v1 import ARTIFACT_DIR
from scripts import v2_core_stage_b1a1e_primary_gate_validation2_input_v1 as base


def generate_all(artifact_dir: Path = ARTIFACT_DIR):
    original = (
        base.SELECTION_SEED,
        base.EXCLUSION_MANIFESTS,
        base.OUTPUT_DIRECTORY,
        base.PROGRAM_GATE_DIRECTORY,
        base.OUTPUT_MANIFEST,
        base.OUTPUT_SELECTION,
    )
    try:
        base.SELECTION_SEED = "B1A1E-R4-PRIMARY-OBJECTIVE-GATE-FRESH-VALIDATION-R4"
        base.EXCLUSION_MANIFESTS = base.EXCLUSION_MANIFESTS + (
            "stage_b1a1e_primary_gate_validation2_execution_manifest_r1.json",
            "stage_b1a1e_primary_gate_validation3_execution_manifest_r1.json",
        )
        base.OUTPUT_DIRECTORY = "stage_b1a1e_primary_gate_validation4_input_packets_r1"
        base.PROGRAM_GATE_DIRECTORY = "stage_b1a1e_primary_gate_validation4_program_inputs_r1"
        base.OUTPUT_MANIFEST = "stage_b1a1e_primary_gate_validation4_input_manifest_r1.json"
        base.OUTPUT_SELECTION = "stage_b1a1e_primary_gate_validation4_selection_r1.json"
        return base.generate_all(artifact_dir)
    finally:
        (
            base.SELECTION_SEED,
            base.EXCLUSION_MANIFESTS,
            base.OUTPUT_DIRECTORY,
            base.PROGRAM_GATE_DIRECTORY,
            base.OUTPUT_MANIFEST,
            base.OUTPUT_SELECTION,
        ) = original


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    print(json.dumps(generate_all(args.artifact_dir), ensure_ascii=False, indent=2))
