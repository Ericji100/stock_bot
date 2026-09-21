"""Freeze the teacher-blind R7 role-qualification execution matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_legacy_role_qualification_r7 import build_schema, transport_schema
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, load_json, sha256_path, write_new_or_identical
from scripts.v2_core_legacy_role_shortlist_r7_v2 import OUTPUT_MANIFEST as SHORTLIST_MANIFEST


VERSION = "v2-core-legacy-role-qualification-execution-r7-candidate-r1"
OUTPUT_MANIFEST = "legacy_role_qualification_execution_manifest_candidate_r7.json"
INPUT_MANIFEST = "legacy_anchor_alignment_input_manifest_candidate_r1.json"
PROMPT_FILE = "v2_core_legacy_role_qualification.prompt.candidate_r7.md"
BUDGET_FILE = "operational_budget_override_v15.json"
MODEL = "gpt-5.6-sol"
REASONING = "xhigh"


def build_manifest(artifact_dir: Path) -> dict[str, Any]:
    input_path = artifact_dir / INPUT_MANIFEST
    shortlist_path = artifact_dir / SHORTLIST_MANIFEST
    prompt_path = artifact_dir / PROMPT_FILE
    budget_path = artifact_dir / BUDGET_FILE
    contract_path = ROOT / "scripts" / "v2_core_legacy_role_qualification_r7.py"
    source = load_json(input_path)
    shortlists = load_json(shortlist_path)
    budget = load_json(budget_path)
    if source["formal_model"] != MODEL or source["reasoning_effort"] != REASONING:
        raise ValueError("input manifest model/effort mismatch")
    if budget["stop_new_ai_calls_when_remaining_percent_lte"] != 70:
        raise ValueError("operational budget policy mismatch")
    if shortlists["teacher_answers_read"] or shortlists["future_performance_used"]:
        raise ValueError("shortlist manifest violates teacher blindness")
    source_by_id = {row["review_id"]: row for row in source["rows"]}
    shortlist_by_id = {row["review_id"]: row for row in shortlists["rows"]}
    if len(source_by_id) != source["case_count"] or set(source_by_id) != set(shortlist_by_id):
        raise ValueError("source/shortlist case set mismatch")

    rows: list[dict[str, Any]] = []
    for review_id in sorted(source_by_id):
        src_row = source_by_id[review_id]
        short_row = shortlist_by_id[review_id]
        packet_path = artifact_dir / source["input_packet_directory"] / src_row["input_packet_file"]
        role_path = artifact_dir / shortlists["shortlist_directory"] / short_row["shortlist_file"]
        if sha256_path(packet_path) != src_row["input_packet_sha256"]:
            raise ValueError(f"input packet hash mismatch: {review_id}")
        if sha256_path(role_path) != short_row["shortlist_sha256"]:
            raise ValueError(f"shortlist hash mismatch: {review_id}")
        packet = load_json(packet_path)
        shortlist = load_json(role_path)
        if packet["review_id"] != review_id or shortlist["review_id"] != review_id:
            raise ValueError(f"packet identity mismatch: {review_id}")
        if packet["as_of"] != src_row["as_of"] or shortlist["as_of"] != src_row["as_of"]:
            raise ValueError(f"packet as-of mismatch: {review_id}")
        for role in sorted(shortlist["roles"]):
            schema = transport_schema(build_schema(packet, shortlist, role))
            rows.append({
                "review_id": review_id,
                "as_of": packet["as_of"],
                "role": role,
                "input_packet_file": src_row["input_packet_file"],
                "input_packet_sha256": sha256_path(packet_path),
                "shortlist_file": short_row["shortlist_file"],
                "shortlist_sha256": sha256_path(role_path),
                "candidate_count": shortlist["roles"][role]["shortlist_count"],
                "transport_schema_sha256": hashlib.sha256(canonical_bytes(schema)).hexdigest(),
            })
    return {
        "manifest_version": VERSION,
        "status": "CANDIDATE_NOT_FROZEN（候選、尚未凍結）",
        "milestone": "MILESTONE_2A／ONE_PASS_LEGACY_ALIGNMENT",
        "case_count": len(source_by_id),
        "role_stage_count": len(rows),
        "input_manifest_file": INPUT_MANIFEST,
        "input_manifest_sha256": sha256_path(input_path),
        "input_packet_directory": source["input_packet_directory"],
        "shortlist_manifest_file": SHORTLIST_MANIFEST,
        "shortlist_manifest_sha256": sha256_path(shortlist_path),
        "shortlist_directory": shortlists["shortlist_directory"],
        "prompt_file": PROMPT_FILE,
        "prompt_sha256": sha256_path(prompt_path),
        "qualification_contract_file": contract_path.name,
        "qualification_contract_sha256": sha256_path(contract_path),
        "budget_file": BUDGET_FILE,
        "budget_sha256": sha256_path(budget_path),
        "stop_remaining_percent_lte": 70,
        "formal_model": MODEL,
        "reasoning_effort": REASONING,
        "required_rounds": 1,
        "formal_ai_calls": 0,
        "teacher_answers_read": False,
        "teacher_values_sent_to_ai": False,
        "locked_set_opened": False,
        "future_performance_used": False,
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    manifest = build_manifest(args.artifact_dir)
    write_new_or_identical(args.artifact_dir / OUTPUT_MANIFEST, canonical_bytes(manifest))
    print(json.dumps({"status": manifest["status"], "role_stage_count": manifest["role_stage_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
