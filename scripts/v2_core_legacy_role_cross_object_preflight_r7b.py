"""Reverify all R7B roles against the same frozen episode before relation AI."""

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
from scripts.v2_core_legacy_role_adjudication_runner_r7b import validate_existing as validate_r7b_adjudication
from scripts.v2_core_legacy_role_qualification_runner_r7 import _bound_inputs
from scripts.v2_core_legacy_role_qualification_runner_r7b import frozen_episode, validate_existing as validate_r7b
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, write_new_or_identical


VERSION = "v2-core-legacy-role-cross-object-preflight-r7b-candidate-r1"


def build_report(artifact_dir: Path, review_id: str) -> dict[str, Any]:
    _manifest, _row, packet, shortlist = _bound_inputs(artifact_dir, review_id, "CURRENT_EPISODE_UP")
    episode, _paths = frozen_episode(artifact_dir, review_id)
    roles = {"CURRENT_EPISODE_UP": episode}
    for role in sorted(shortlist["roles"]):
        if role != "CURRENT_EPISODE_UP":
            qualification = validate_r7b(artifact_dir=artifact_dir, review_id=review_id, role=role)
            if qualification["resolution_reason"] == "MULTIPLE_NON_EQUIVALENT_ELIGIBLE_CANDIDATES":
                roles[role] = validate_r7b_adjudication(artifact_dir=artifact_dir, review_id=review_id, role=role)
            else:
                roles[role] = qualification
    selected = selected_role_rows(roles, shortlist)
    contradictions = check_cross_objects(selected)
    if any(result.get("bound_current_episode_candidate_id") != episode["selected_candidate_id"] for role, result in roles.items() if role != "CURRENT_EPISODE_UP"):
        contradictions.append({"code": "EPISODE_BINDING_MISMATCH", "details": "R7B role did not bind frozen episode"})
    return {
        "report_version": VERSION,
        "review_id": review_id,
        "as_of": packet["as_of"],
        "status": "CROSS_ROLE_UNRESOLVED_NO_TRADE" if contradictions else "CROSS_ROLE_PRELIMINARY_READY",
        "role_results": roles,
        "selected_role_objects": selected,
        "contradictions": contradictions,
        "formal_ai_calls_by_this_gate": 0,
        "teacher_answers_read": False,
        "locked_set_opened": False,
        "future_performance_used": False,
        "trade_permission_granted": False,
        "interpretation": "Roles are coherent for one AS-OF case; relation atoms, route, trade path and teacher comparison remain untested.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    parser.add_argument("--review-id", required=True)
    args = parser.parse_args()
    report = build_report(args.artifact_dir, args.review_id)
    path = args.artifact_dir / "legacy_role_cross_object_preflight_candidate_r7b" / f"{args.review_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_new_or_identical(path, canonical_bytes(report))
    print(json.dumps({"status": report["status"], "contradiction_count": len(report["contradictions"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
