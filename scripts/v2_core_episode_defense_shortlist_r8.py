"""Teacher-blind AS-OF confirmed small-low candidates for trade-episode defense.

This is candidate generation, not a selected stop or permission to trade.
The bounded 180-calendar-day window is a calibration candidate scope only;
failure to find a suitable defense means broaden/review, never automatic FAIL.
"""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, canonical_bytes, load_json, sha256_path, write_new_or_identical


VERSION = "v2-core-episode-defense-shortlist-r8-candidate-r1"
WINDOW_CALENDAR_DAYS = 180
INPUT_DIRECTORY = "legacy_anchor_alignment_input_packets_candidate_r1"
OUTPUT_DIRECTORY = "episode_defense_shortlists_candidate_r8"


def build_shortlist(packet: dict[str, Any]) -> dict[str, Any]:
    as_of = date.fromisoformat(packet["as_of"])
    if packet.get("blindness_contract", {}).get("no_future_price") is False:
        raise ValueError("packet declares future-price exposure")
    rows = []
    for pivot in packet["confirmed_pivots_to_as_of"]:
        if pivot["scale"] != "SMALL" or pivot["side"] != "LOW":
            continue
        source_date = date.fromisoformat(pivot["source_date"])
        confirmed_date = date.fromisoformat(pivot["confirmation_date"])
        if not (source_date <= confirmed_date <= as_of):
            raise ValueError("future or backwards-confirmed pivot in AS-OF packet")
        if (as_of - source_date).days > WINDOW_CALENDAR_DAYS:
            continue
        source = {
            "source_date": pivot["source_date"],
            "confirmation_date": pivot["confirmation_date"],
            "price": pivot["price"],
            "scale": "SMALL",
            "side": "LOW",
        }
        candidate_id = "DEF-" + hashlib.sha256(canonical_bytes(source)).hexdigest()[:20]
        rows.append({
            "candidate_id": candidate_id,
            **source,
            "evidence_ref": f"DEFENSE_PIVOT:{source['source_date']}:{source['confirmation_date']}:{source['price']}",
            "availability": "CONFIRMED_AT_SIGNAL_CLOSE" if confirmed_date == as_of else "CONFIRMED_BEFORE_SIGNAL_DATE",
        })
    rows.sort(key=lambda item: (item["source_date"], item["confirmation_date"], item["candidate_id"]), reverse=True)
    if len({row["candidate_id"] for row in rows}) != len(rows):
        raise ValueError("duplicate defense candidate")
    return {
        "shortlist_version": VERSION,
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "window_calendar_days": WINDOW_CALENDAR_DAYS,
        "candidate_count": len(rows),
        "candidate_rows": rows,
        "selected_candidate_id": None,
        "trade_permission_granted": False,
        "teacher_answers_read": False,
        "future_performance_used": False,
        "interpretation": "AS-OF candidate visibility only; AI must assess causal structure/stop, with broader review if no candidate qualifies.",
    }


def build_for_file(packet_path: Path) -> dict[str, Any]:
    packet = load_json(packet_path)
    result = build_shortlist(packet)
    result["input_packet_sha256"] = sha256_path(packet_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    parser.add_argument("--review-id")
    args = parser.parse_args()
    input_dir = args.artifact_dir / INPUT_DIRECTORY
    if args.review_id:
        packets = [input_dir / f"{args.review_id}.json"]
    else:
        packets = sorted(input_dir.glob("FP-*.json"))
    if not packets:
        raise ValueError("no AS-OF packets")
    results = []
    for packet_path in packets:
        report = build_for_file(packet_path)
        output = args.artifact_dir / OUTPUT_DIRECTORY / packet_path.name
        output.parent.mkdir(parents=True, exist_ok=True)
        write_new_or_identical(output, canonical_bytes(report))
        results.append({"review_id": report["review_id"], "candidate_count": report["candidate_count"]})
    print(json.dumps(results, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
