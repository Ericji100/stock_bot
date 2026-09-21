"""Diagnostic mixed-version preflight for R7B roles and R7C mature transport.

This is not a homogeneous R7C benchmark pass.  Every role's source version is
recorded so that the valid R7B work is not silently represented as R7C work.
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

from scripts.v2_core_legacy_role_cross_object_preflight_r7 import check_cross_objects, selected_role_rows
from scripts.v2_core_legacy_role_adjudication_runner_r7b import output_paths as adjudication_paths, validate_existing as validate_adjudication
from scripts.v2_core_legacy_role_qualification_runner_r7 import _bound_inputs
from scripts.v2_core_legacy_role_qualification_runner_r7b import frozen_episode, output_paths as r7b_paths, validate_existing as validate_r7b
from scripts.v2_core_legacy_role_qualification_runner_r7c import output_paths as r7c_paths, validate_existing as validate_r7c
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, sha256_path, write_new_or_identical


VERSION = "v2-core-legacy-role-cross-object-preflight-r7c-mixed-diagnostic-r1"
R7C_ROLE = "CONTROLLING_MATURE_CAMPAIGN"


def build_report(artifact_dir: Path, review_id: str) -> dict[str, Any]:
    _manifest, _row, packet, shortlist = _bound_inputs(artifact_dir, review_id, "CURRENT_EPISODE_UP")
    episode, episode_paths = frozen_episode(artifact_dir, review_id)
    results = {"CURRENT_EPISODE_UP": episode}
    sources: dict[str, dict[str, Any]] = {
        "CURRENT_EPISODE_UP": {
            "stage": "R7_FROZEN_EPISODE",
            "output_sha256": sha256_path(episode_paths.get("adjudication_output", episode_paths["qualification_output"])),
            "receipt_sha256": sha256_path(episode_paths.get("adjudication_receipt", episode_paths["qualification_receipt"])),
        }
    }
    for role in sorted(shortlist["roles"]):
        if role == "CURRENT_EPISODE_UP":
            continue
        if role == R7C_ROLE:
            qualification = validate_r7c(artifact_dir=artifact_dir, review_id=review_id, role=role)
            output, receipt = r7c_paths(artifact_dir, review_id, role)
            stage = "R7C_FIXED_KEY_QUALIFICATION"
        else:
            qualification = validate_r7b(artifact_dir=artifact_dir, review_id=review_id, role=role)
            output, receipt = r7b_paths(artifact_dir, review_id, role)
            stage = "R7B_EPISODE_BOUND_QUALIFICATION"
        if qualification["resolution_reason"] == "MULTIPLE_NON_EQUIVALENT_ELIGIBLE_CANDIDATES":
            if role == R7C_ROLE:
                raise ValueError("R7C mature role unresolved; diagnostic preflight cannot adjudicate it")
            results[role] = validate_adjudication(artifact_dir=artifact_dir, review_id=review_id, role=role)
            output, receipt = adjudication_paths(artifact_dir, review_id, role)
            stage = "R7B_EPISODE_BOUND_ADJUDICATION"
        else:
            results[role] = qualification
        sources[role] = {
            "stage": stage,
            "output_sha256": sha256_path(output),
            "receipt_sha256": sha256_path(receipt),
        }
    selected = selected_role_rows(results, shortlist)
    contradictions = check_cross_objects(selected)
    if any(result.get("bound_current_episode_candidate_id") != episode["selected_candidate_id"] for role, result in results.items() if role != "CURRENT_EPISODE_UP"):
        contradictions.append({"code": "EPISODE_BINDING_MISMATCH", "details": "role did not bind frozen episode"})
    return {
        "report_version": VERSION,
        "review_id": review_id,
        "as_of": packet["as_of"],
        "status": "CROSS_ROLE_UNRESOLVED_NO_TRADE" if contradictions else "CROSS_ROLE_PRELIMINARY_READY",
        "role_results": results,
        "selected_role_objects": selected,
        "role_source_versions": sources,
        "mixed_version_diagnostic_only": True,
        "contradictions": contradictions,
        "formal_ai_calls_by_this_gate": 0,
        "teacher_answers_read": False,
        "locked_set_opened": False,
        "future_performance_used": False,
        "trade_permission_granted": False,
        "interpretation": "Mixed R7/R7B/R7C diagnostic, not a homogeneous benchmark or legacy replication result.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    parser.add_argument("--review-id", required=True)
    args = parser.parse_args()
    report = build_report(args.artifact_dir, args.review_id)
    path = args.artifact_dir / "legacy_role_cross_object_preflight_candidate_r7c" / f"{args.review_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_new_or_identical(path, canonical_bytes(report))
    print(json.dumps({"status": report["status"], "contradiction_count": len(report["contradictions"]), "mixed_version_diagnostic_only": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
