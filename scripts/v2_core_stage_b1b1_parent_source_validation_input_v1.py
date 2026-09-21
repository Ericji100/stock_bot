"""Build a fresh B1b1 heldout batch excluding all calibration cases."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_objective_candidate_catalog_v1 import ARTIFACT_DIR
from scripts.v2_core_stage_b1a2_active_link_validator_v1 import load_json
from scripts.v2_core_stage_b1b1_parent_source_input_v1 import generate_all as generate_base


CALIBRATION_SELECTION = "stage_b1b1_parent_source_selection_candidate_r1.json"
SELECTION_SEED = "B1B1-PARENT-SOURCE-HELDOUT-R1"
OUTPUT_DIRECTORY = "stage_b1b1_parent_source_validation_input_packets_r1"
OUTPUT_MANIFEST = "stage_b1b1_parent_source_validation_input_manifest_r1.json"
SELECTION_FILE = "stage_b1b1_parent_source_validation_selection_r1.json"


def generate_all(artifact_dir: Path = ARTIFACT_DIR) -> dict:
    calibration = load_json(artifact_dir / CALIBRATION_SELECTION)
    excluded = frozenset(str(row["review_id"]) for row in calibration["rows"])
    return generate_base(
        artifact_dir,
        selection_seed=SELECTION_SEED,
        case_count=5,
        excluded_review_ids=excluded,
        output_directory=OUTPUT_DIRECTORY,
        output_manifest=OUTPUT_MANIFEST,
        selection_file=SELECTION_FILE,
        packet_version="v2-core-stage-b1b1-parent-source-heldout-input-r1",
        selection_version="v2-core-stage-b1b1-parent-source-heldout-selection-r1",
        manifest_version="v2-core-stage-b1b1-parent-source-heldout-input-r1",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    print(json.dumps(generate_all(args.artifact_dir), ensure_ascii=False, indent=2))
