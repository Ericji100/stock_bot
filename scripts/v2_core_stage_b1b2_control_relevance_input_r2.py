"""Build evidence-complete B1b2 R2 calibration inputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_objective_candidate_catalog_v1 import ARTIFACT_DIR
from scripts.v2_core_stage_b1b2_control_relevance_input_v1 import generate_all as generate_base


OUTPUT_DIRECTORY = "stage_b1b2_control_relevance_input_packets_candidate_r2"
OUTPUT_MANIFEST = "stage_b1b2_control_relevance_input_manifest_candidate_r2.json"


def generate_all(artifact_dir: Path = ARTIFACT_DIR) -> dict:
    return generate_base(
        artifact_dir,
        output_directory=OUTPUT_DIRECTORY,
        output_manifest=OUTPUT_MANIFEST,
        packet_version="v2-core-stage-b1b2-control-relevance-input-r2-candidate",
        manifest_version="v2-core-stage-b1b2-control-relevance-input-r2-candidate",
        include_relation_path_segments=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    print(json.dumps(generate_all(args.artifact_dir), ensure_ascii=False, indent=2))
