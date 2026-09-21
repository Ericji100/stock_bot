"""R7 role shortlist representation revision: preserve forming structures.

R1 is immutable.  Formation-bearing roles now retain every FORMING candidate
when within the predeclared role cap, then use the same objective balanced
selection for the remaining CONFIRMED candidates.  No teacher values enter.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import v2_core_legacy_role_shortlist_r7 as r1


VERSION = "v2-core-legacy-role-shortlist-r7-candidate-r2"
OUTPUT_DIRECTORY = "legacy_role_shortlists_candidate_r7_v2"
OUTPUT_MANIFEST = "legacy_role_shortlist_manifest_candidate_r7_v2.json"
FROZEN_R1_SHA256 = "2cbce656ef1dd34629f362921c6aaf704d01debf4894841f6c12f3538c80e2e6"
FORMATION_BEARING_ROLES = (
    "ACTIVE_DOWN_CONTROLLER",
    "UP_CONTROL_CHALLENGER",
    "CONTROLLING_MATURE_CAMPAIGN",
)


def candidate_row(item: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
    return {
        "candidate_id": item["candidate_id"],
        "evidence_option_id": item["evidence_option_id"],
        "direction": item["direction"],
        "status": item["status"],
        "basis": item["basis"],
        "scale": item["scale"],
        "start_date": item["start_date"],
        "confirmed_end_date": item["confirmed_end_date"],
        "observed_through": item["observed_through"],
        "source_evidence_refs": item["source_evidence_refs"],
        "selection_reasons": reasons,
    }


def build_shortlists(packet: dict[str, Any]) -> dict[str, Any]:
    result = r1.build_shortlists(packet)
    for role in FORMATION_BEARING_ROLES:
        universe = r1.role_universe(role, packet["candidate_pool"])
        forming = sorted(
            (item for item in universe if item["status"] == "FORMING"),
            key=lambda item: item["candidate_id"],
        )
        limit = r1.ROLE_LIMITS[role]
        if len(forming) > limit:
            # No silent exclusion of forming structures.  A future larger pool
            # needs its own representation version and coverage audit.
            raise ValueError(f"forming candidate overflow for {role}")
        confirmed = [item for item in universe if item["status"] == "CONFIRMED"]
        remaining = limit - len(forming)
        supplemental = r1.balanced_shortlist(confirmed, remaining) if remaining else []
        selected = [(item, ["ALL_FORMING_IN_ROLE_UNIVERSE"]) for item in forming] + supplemental
        selected.sort(key=lambda pair: pair[0]["candidate_id"])
        result["roles"][role] = {
            "objective_universe_count": len(universe),
            "shortlist_count": len(selected),
            "truncated": len(universe) > len(selected),
            "candidate_rows": [candidate_row(item, reasons) for item, reasons in selected],
            "objective_equivalence_groups": r1.equivalence_groups([item for item, _ in selected]),
        }
    result["shortlist_version"] = VERSION
    result["revision_from_r1"] = "PRESERVE_ALL_FORMING_CANDIDATES_IN_FORMATION_BEARING_ROLES"
    return result


def build_all(artifact_dir: Path) -> dict[str, Any]:
    if r1.sha256_path(Path(r1.__file__)) != FROZEN_R1_SHA256:
        raise ValueError("R1 generator changed after freeze")
    input_manifest_path = artifact_dir / r1.INPUT_MANIFEST
    source_manifest = r1.load_json(input_manifest_path)
    output_dir = artifact_dir / OUTPUT_DIRECTORY
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for source in source_manifest["rows"]:
        packet_path = artifact_dir / source_manifest["input_packet_directory"] / source["input_packet_file"]
        if r1.sha256_path(packet_path) != source["input_packet_sha256"]:
            raise ValueError(f"source packet hash mismatch: {source['review_id']}")
        packet = r1.load_json(packet_path)
        if packet["review_id"] != source["review_id"] or packet["as_of"] != source["as_of"]:
            raise ValueError(f"source packet binding mismatch: {source['review_id']}")
        result = build_shortlists(packet)
        result["source_packet_sha256"] = r1.sha256_path(packet_path)
        output_path = output_dir / f"{source['review_id']}.json"
        r1.write_new_or_identical(output_path, r1.canonical_bytes(result))
        rows.append({
            "review_id": source["review_id"],
            "as_of": source["as_of"],
            "source_packet_sha256": r1.sha256_path(packet_path),
            "shortlist_file": output_path.name,
            "shortlist_sha256": r1.sha256_path(output_path),
            "role_counts": {role: data["shortlist_count"] for role, data in result["roles"].items()},
        })
    manifest = {
        "manifest_version": VERSION,
        "status": "CANDIDATE_NOT_FROZEN（候選、尚未凍結）",
        "input_manifest_file": r1.INPUT_MANIFEST,
        "input_manifest_sha256": r1.sha256_path(input_manifest_path),
        "shortlist_directory": OUTPUT_DIRECTORY,
        "prior_generator_file": Path(r1.__file__).name,
        "prior_generator_sha256": FROZEN_R1_SHA256,
        "case_count": len(rows),
        "formal_ai_calls": 0,
        "teacher_answers_read": False,
        "locked_set_read": False,
        "future_performance_used": False,
        "rows": sorted(rows, key=lambda row: row["review_id"]),
    }
    r1.write_new_or_identical(artifact_dir / OUTPUT_MANIFEST, r1.canonical_bytes(manifest))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=r1.ARTIFACT_DIR)
    args = parser.parse_args()
    result = build_all(args.artifact_dir)
    print(json.dumps({"status": result["status"], "case_count": result["case_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
