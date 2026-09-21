"""Teacher-blind grouping of AS-OF work-anchor candidates by basis/direction/lifecycle.

Grouping guarantees each source family is considered separately.  It does
not rank, select or authorize any candidate or trade.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_episode_defense_shortlist_r8 import INPUT_DIRECTORY
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, load_json, sha256_path, write_new_or_identical


VERSION = "v2-core-anchor-basis-groups-r9-candidate-r1"
OUTPUT_DIRECTORY = "anchor_basis_groups_candidate_r9"


def build_groups(packet: dict[str, Any]) -> dict[str, Any]:
    as_of = date.fromisoformat(packet["as_of"])
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    seen = set()
    for candidate in packet["candidate_pool"]:
        candidate_id = candidate["candidate_id"]
        if candidate_id in seen:
            raise ValueError("duplicate anchor candidate ID")
        seen.add(candidate_id)
        start = date.fromisoformat(candidate["start_date"])
        if start > as_of:
            raise ValueError("future anchor start")
        if candidate["status"] == "CONFIRMED":
            end = candidate.get("confirmed_end_date")
            if not end or not start <= date.fromisoformat(end) <= as_of:
                raise ValueError("confirmed anchor not complete by AS-OF")
        elif candidate["status"] == "FORMING":
            if candidate.get("confirmed_end_date") is not None or candidate.get("observed_through") != packet["as_of"]:
                raise ValueError("forming anchor not AS-OF bounded")
        else:
            raise ValueError("unknown candidate lifecycle")
        group_key = (candidate["basis"], candidate["direction"], candidate["status"])
        grouped[group_key].append(candidate)
    groups = []
    for basis, direction, status in sorted(grouped):
        key = {"basis": basis, "direction": direction, "status": status}
        group_id = "AGRP-" + hashlib.sha256(canonical_bytes(key)).hexdigest()[:20]
        candidates = sorted(grouped[(basis, direction, status)], key=lambda item: (item["start_date"], item["candidate_id"]), reverse=True)
        groups.append({
            "group_id": group_id,
            **key,
            "candidate_count": len(candidates),
            "candidate_ids": [item["candidate_id"] for item in candidates],
        })
    return {
        "group_version": VERSION,
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "group_count": len(groups),
        "candidate_count": len(seen),
        "groups": groups,
        "selected_candidates": None,
        "teacher_answers_read": False,
        "future_performance_used": False,
        "trade_permission_granted": False,
    }


def build_for_file(packet_path: Path) -> dict[str, Any]:
    packet = load_json(packet_path)
    result = build_groups(packet)
    result["input_packet_sha256"] = sha256_path(packet_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    parser.add_argument("--review-id")
    args = parser.parse_args()
    input_dir = args.artifact_dir / INPUT_DIRECTORY
    packets = [input_dir / f"{args.review_id}.json"] if args.review_id else sorted(input_dir.glob("FP-*.json"))
    if not packets:
        raise ValueError("no AS-OF packets")
    rows = []
    for packet_path in packets:
        report = build_for_file(packet_path)
        output = args.artifact_dir / OUTPUT_DIRECTORY / packet_path.name
        output.parent.mkdir(parents=True, exist_ok=True)
        write_new_or_identical(output, canonical_bytes(report))
        rows.append({"review_id": report["review_id"], "group_count": report["group_count"], "candidate_count": report["candidate_count"]})
    print(json.dumps(rows, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
