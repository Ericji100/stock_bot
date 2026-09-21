"""Build outcome-blind, role-specific candidate shortlists for R7.

This is a presentation layer over the frozen Campaign R2 pool.  Direction,
completion state, dates, basis family and objective metrics may select rows;
no course role, scenario, teacher answer or trade permission is inferred here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
VERSION = "v2-core-legacy-role-shortlist-r7-candidate-r1"
OUTPUT_DIRECTORY = "legacy_role_shortlists_candidate_r7"
OUTPUT_MANIFEST = "legacy_role_shortlist_manifest_candidate_r7.json"
INPUT_MANIFEST = "legacy_anchor_alignment_input_manifest_candidate_r1.json"

ROLE_LIMITS = {
    "ACTIVE_DOWN_CONTROLLER": 48,
    "CURRENT_EPISODE_UP": 40,
    "UP_CONTROL_CHALLENGER": 56,
    "IMMEDIATE_COMPLETED_UP_PARENT": 48,
    "CONTROLLING_MATURE_CAMPAIGN": 56,
}


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def basis_family(candidate: dict[str, Any]) -> str:
    basis = str(candidate["basis"])
    if basis.startswith("MACD_"):
        return "MACD"
    if basis.startswith("PIVOT_"):
        return "PIVOT"
    raise ValueError(f"unsupported candidate basis: {basis}")


def role_universe(role: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if role == "ACTIVE_DOWN_CONTROLLER":
        return [item for item in candidates if item["direction"] == "DOWN"]
    if role == "CURRENT_EPISODE_UP":
        return [item for item in candidates if item["direction"] == "UP" and item["status"] == "FORMING"]
    if role == "IMMEDIATE_COMPLETED_UP_PARENT":
        return [item for item in candidates if item["direction"] == "UP" and item["status"] == "CONFIRMED"]
    if role in ("UP_CONTROL_CHALLENGER", "CONTROLLING_MATURE_CAMPAIGN"):
        return [item for item in candidates if item["direction"] == "UP"]
    raise ValueError(f"unknown role: {role}")


def objective_rankings(candidates: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Four complementary objective views; none assign a course role."""
    return {
        "RECENT_ENDPOINT": sorted(
            candidates,
            key=lambda item: (item["observed_through"], item["start_date"], item["candidate_id"]),
            reverse=True,
        ),
        "RECENT_START": sorted(
            candidates,
            key=lambda item: (item["start_date"], item["observed_through"], item["candidate_id"]),
            reverse=True,
        ),
        "LONG_DURATION": sorted(
            candidates,
            key=lambda item: (item["objective_metrics"]["trading_bars"], item["observed_through"], item["candidate_id"]),
            reverse=True,
        ),
        "LARGE_ATR_MOVE": sorted(
            candidates,
            key=lambda item: (item["objective_metrics"]["directional_move_atr_at_start"], item["observed_through"], item["candidate_id"]),
            reverse=True,
        ),
    }


def balanced_shortlist(candidates: list[dict[str, Any]], limit: int) -> list[tuple[dict[str, Any], list[str]]]:
    """Round-robin objective views, reserving capacity for both basis families."""
    if len(candidates) <= limit:
        return [(item, ["ROLE_UNIVERSE_COMPLETE"]) for item in sorted(candidates, key=lambda row: row["candidate_id"])]

    chosen: dict[str, tuple[dict[str, Any], set[str]]] = {}
    by_family = {family: [item for item in candidates if basis_family(item) == family] for family in ("MACD", "PIVOT")}

    def fill(pool: list[dict[str, Any]], capacity: int) -> None:
        rankings = objective_rankings(pool)
        order = tuple(rankings)
        index = {name: 0 for name in order}
        while len(chosen) < capacity:
            changed = False
            for name in order:
                ranked = rankings[name]
                while index[name] < len(ranked) and ranked[index[name]]["candidate_id"] in chosen:
                    index[name] += 1
                if index[name] >= len(ranked):
                    continue
                item = ranked[index[name]]
                index[name] += 1
                chosen[item["candidate_id"]] = (item, {name, f"BASIS_{basis_family(item)}"})
                changed = True
                if len(chosen) >= capacity:
                    break
            if not changed:
                break

    # Neither MACD nor pivot is automatically preferred as a course anchor.
    family_budget = limit // 2
    fill(by_family["MACD"], min(family_budget, len(by_family["MACD"])))
    fill(by_family["PIVOT"], min(limit, len(chosen) + family_budget))
    fill(candidates, limit)
    return [(item, sorted(reasons)) for item, reasons in (chosen[key] for key in sorted(chosen))]


def equivalence_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[str]] = {}
    for item in rows:
        key = (
            item["direction"], item["status"], item["scale"], item["start_date"],
            item["confirmed_end_date"], item["observed_through"], item["start_price"],
            item["observed_end_price"],
        )
        groups.setdefault(key, []).append(item["candidate_id"])
    return [
        {"candidate_ids": sorted(ids), "equivalence_basis": "EXACT_OBJECTIVE_BOUNDARIES_AND_PRICES"}
        for ids in groups.values() if len(ids) > 1
    ]


def build_shortlists(packet: dict[str, Any]) -> dict[str, Any]:
    candidates = packet["candidate_pool"]
    if len({item["candidate_id"] for item in candidates}) != len(candidates):
        raise ValueError("duplicate source candidate IDs")
    if any(item["observed_through"] > packet["as_of"] for item in candidates):
        raise ValueError("post-AS-OF candidate")
    if any(item["confirmed_end_date"] and item["confirmed_end_date"] > packet["as_of"] for item in candidates):
        raise ValueError("post-AS-OF confirmed endpoint")

    roles: dict[str, Any] = {}
    for role, limit in ROLE_LIMITS.items():
        universe = role_universe(role, candidates)
        shortlist = balanced_shortlist(universe, limit)
        selected = [item for item, _ in shortlist]
        roles[role] = {
            "objective_universe_count": len(universe),
            "shortlist_count": len(selected),
            "truncated": len(universe) > len(selected),
            "candidate_rows": [
                {
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
                for item, reasons in shortlist
            ],
            "objective_equivalence_groups": equivalence_groups(selected),
        }
    return {
        "shortlist_version": VERSION,
        "status": "CANDIDATE_NOT_FROZEN（候選、尚未凍結）",
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "source_packet_sha256": None,
        "role_limits": ROLE_LIMITS,
        "roles": roles,
        "contract": {
            "teacher_answers_read": False,
            "locked_set_read": False,
            "future_performance_used": False,
            "course_role_selected_by_program": False,
            "scenario_or_trade_permission_derived": False,
        },
    }


def write_new_or_identical(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise FileExistsError(f"refusing to overwrite non-identical artifact: {path}")
        return
    path.write_bytes(payload)


def build_all(artifact_dir: Path) -> dict[str, Any]:
    manifest_path = artifact_dir / INPUT_MANIFEST
    manifest = load_json(manifest_path)
    output_dir = artifact_dir / OUTPUT_DIRECTORY
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for source in manifest["rows"]:
        packet_path = artifact_dir / manifest["input_packet_directory"] / source["input_packet_file"]
        if sha256_path(packet_path) != source["input_packet_sha256"]:
            raise ValueError(f"input packet hash mismatch: {source['review_id']}")
        packet = load_json(packet_path)
        if packet["review_id"] != source["review_id"] or packet["as_of"] != source["as_of"]:
            raise ValueError(f"input packet identity mismatch: {source['review_id']}")
        result = build_shortlists(packet)
        result["source_packet_sha256"] = sha256_path(packet_path)
        output_path = output_dir / f"{source['review_id']}.json"
        write_new_or_identical(output_path, canonical_bytes(result))
        rows.append({
            "review_id": source["review_id"],
            "as_of": source["as_of"],
            "source_packet_sha256": sha256_path(packet_path),
            "shortlist_file": output_path.name,
            "shortlist_sha256": sha256_path(output_path),
            "role_counts": {role: data["shortlist_count"] for role, data in result["roles"].items()},
        })
    output_manifest = {
        "manifest_version": VERSION,
        "status": "CANDIDATE_NOT_FROZEN（候選、尚未凍結）",
        "input_manifest_file": INPUT_MANIFEST,
        "input_manifest_sha256": sha256_path(manifest_path),
        "shortlist_directory": OUTPUT_DIRECTORY,
        "case_count": len(rows),
        "formal_ai_calls": 0,
        "teacher_answers_read": False,
        "locked_set_read": False,
        "future_performance_used": False,
        "rows": sorted(rows, key=lambda row: row["review_id"]),
    }
    write_new_or_identical(artifact_dir / OUTPUT_MANIFEST, canonical_bytes(output_manifest))
    return output_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    result = build_all(args.artifact_dir)
    print(json.dumps({"status": result["status"], "case_count": result["case_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
