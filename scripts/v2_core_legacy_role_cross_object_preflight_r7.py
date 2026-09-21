"""Deterministic AS-OF causal consistency gate across five R7 role objects.

This gate does not change model atoms or select replacement objects.  Any
impossible parent/episode chronology stays visible and blocks route/trade.
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

from scripts.v2_core_legacy_role_adjudication_runner_r7 import validate_existing as validate_adjudication
from scripts.v2_core_legacy_role_qualification_runner_r7 import _bound_inputs, validate_existing as validate_qualification
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, write_new_or_identical


VERSION = "v2-core-legacy-role-cross-object-preflight-r7-candidate-r1"


def selected_role_rows(role_results: dict[str, dict[str, Any]], shortlist: dict[str, Any]) -> dict[str, dict[str, Any] | None]:
    selected: dict[str, dict[str, Any] | None] = {}
    for role, result in role_results.items():
        candidate_id = result["selected_candidate_id"]
        if result["role_status"] != "SELECTED" or not candidate_id:
            selected[role] = None
            continue
        matches = [row for row in shortlist["roles"][role]["candidate_rows"] if row["candidate_id"] == candidate_id]
        if len(matches) != 1:
            raise ValueError(f"selected candidate not unique in shortlist: {role}")
        selected[role] = matches[0]
    return selected


def check_cross_objects(selected: dict[str, dict[str, Any] | None]) -> list[dict[str, Any]]:
    contradictions: list[dict[str, Any]] = []
    episode = selected["CURRENT_EPISODE_UP"]
    parent = selected["IMMEDIATE_COMPLETED_UP_PARENT"]
    if episode is None:
        contradictions.append({"code": "CURRENT_EPISODE_UNRESOLVED", "details": "No unique current UP episode object"})
        return contradictions
    if episode["direction"] != "UP" or episode["status"] != "FORMING":
        contradictions.append({"code": "CURRENT_EPISODE_OBJECT_INVALID", "details": "Current episode must be forming UP"})
    if parent is not None:
        if parent["direction"] != "UP" or parent["status"] != "CONFIRMED" or not parent["confirmed_end_date"]:
            contradictions.append({"code": "IMMEDIATE_PARENT_OBJECT_INVALID", "details": "Parent must be completed UP"})
        elif parent["confirmed_end_date"] >= episode["start_date"]:
            contradictions.append({
                "code": "IMMEDIATE_PARENT_NOT_BEFORE_EPISODE",
                "details": "A direct completed parent must finish before the episode it parents begins",
                "parent_candidate_id": parent["candidate_id"],
                "parent_confirmed_end_date": parent["confirmed_end_date"],
                "episode_candidate_id": episode["candidate_id"],
                "episode_start_date": episode["start_date"],
            })
    return contradictions


def build_report(artifact_dir: Path, review_id: str) -> dict[str, Any]:
    # Resolve each prior immutable AI output and receipt from disk first.
    _manifest, _row, packet, shortlist = _bound_inputs(artifact_dir, review_id, "CURRENT_EPISODE_UP")
    role_results: dict[str, dict[str, Any]] = {}
    for role in sorted(shortlist["roles"]):
        qualification = validate_qualification(artifact_dir=artifact_dir, review_id=review_id, role=role)
        if qualification["resolution_reason"] == "MULTIPLE_NON_EQUIVALENT_ELIGIBLE_CANDIDATES":
            role_results[role] = validate_adjudication(artifact_dir=artifact_dir, review_id=review_id, role=role)
        else:
            role_results[role] = qualification
    selected = selected_role_rows(role_results, shortlist)
    contradictions = check_cross_objects(selected)
    return {
        "report_version": VERSION,
        "review_id": review_id,
        "as_of": packet["as_of"],
        "status": "CROSS_ROLE_UNRESOLVED_NO_TRADE" if contradictions else "CROSS_ROLE_PRELIMINARY_READY",
        "role_results": role_results,
        "selected_role_objects": selected,
        "contradictions": contradictions,
        "formal_ai_calls_by_this_gate": 0,
        "teacher_answers_read": False,
        "locked_set_opened": False,
        "future_performance_used": False,
        "trade_permission_granted": False,
        "interpretation": "Preliminary role coherence only; relation atoms, scenario and trade path are not yet evaluated.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    parser.add_argument("--review-id", required=True)
    args = parser.parse_args()
    report = build_report(args.artifact_dir, args.review_id)
    path = args.artifact_dir / "legacy_role_cross_object_preflight_candidate_r7" / f"{args.review_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_new_or_identical(path, canonical_bytes(report))
    print(json.dumps({"status": report["status"], "contradiction_count": len(report["contradictions"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
