"""Compress Campaign R1 candidates into an outcome-blind alignment pool.

R1 remains immutable.  This additive R2 layer retains the original shortlist
and supplements it with scale-balanced forming candidates and candidates that
terminate at recent confirmed LARGE pivots.  It does not read legacy answers,
calibration labels, identity, or future performance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT
    / "reports"
    / "course_backtest"
    / "2026-09-10"
    / "v2_core_reproducible_goal_v1"
)
POOL_VERSION = "v2-core-campaign-candidate-pool-r2-candidate"
OUTPUT_DIRECTORY = "campaign_candidate_pools_candidate_r2"
MAX_POOL_SIZE = 160


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def build_pool(catalog: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    candidates = {item["candidate_id"]: item for item in catalog["campaign_candidates"]}
    selected: dict[str, set[str]] = {}

    def add(items: list[dict[str, Any]], reason: str) -> None:
        for item in items:
            selected.setdefault(str(item["candidate_id"]), set()).add(reason)

    add(
        [candidates[candidate_id] for candidate_id in catalog["prompt_shortlist_campaign_ids"]],
        "CAMPAIGN_R1_SHORTLIST",
    )

    # Balance FORMING pivot candidates by scale.  The R1 mixed-scale ranking can
    # otherwise suppress a valid LARGE or SMALL representation.
    for scale in ("LARGE", "SMALL"):
        rows = [
            item
            for item in candidates.values()
            if item["basis"] == "PIVOT_FORMING_CAMPAIGN" and item["scale"] == scale
        ]
        add(
            sorted(
                rows,
                key=lambda item: (item["start_date"], item["candidate_id"]),
                reverse=True,
            )[:8],
            f"FORMING_{scale}_RECENT",
        )
        add(
            sorted(
                rows,
                key=lambda item: (
                    item["objective_metrics"]["trading_bars"],
                    item["objective_metrics"]["directional_move_pct"],
                    item["candidate_id"],
                ),
                reverse=True,
            )[:6],
            f"FORMING_{scale}_DURATION",
        )

    # Recent LARGE pivot endpoints are objective control-boundary candidates.
    # For each endpoint, retain the three most recent starts in every
    # direction/scale group.  This is a structural grouping, not a legacy-label
    # lookup.
    large_end_dates = sorted(
        {
            str(item["source_date"])
            for item in packet["confirmed_pivots_to_as_of"]
            if item.get("scale") == "LARGE"
            and str(item["confirmation_date"]) <= str(packet["as_of"])
        },
        reverse=True,
    )[:6]
    for end_date in large_end_dates:
        endpoint_rows = [
            item
            for item in candidates.values()
            if item["basis"] == "PIVOT_CONFIRMED_CAMPAIGN"
            and item["confirmed_end_date"] == end_date
        ]
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in endpoint_rows:
            groups.setdefault((str(item["direction"]), str(item["scale"])), []).append(item)
        for (direction, scale), rows in groups.items():
            add(
                sorted(
                    rows,
                    key=lambda item: (item["start_date"], item["candidate_id"]),
                    reverse=True,
                )[:3],
                f"RECENT_LARGE_ENDPOINT:{end_date}:{direction}:{scale}",
            )

    # Recent confirmed candidates are also balanced by scale.
    for scale in ("LARGE", "SMALL"):
        rows = [
            item
            for item in candidates.values()
            if item["basis"] == "PIVOT_CONFIRMED_CAMPAIGN" and item["scale"] == scale
        ]
        add(
            sorted(
                rows,
                key=lambda item: (
                    item["confirmed_end_date"] or "",
                    item["start_date"],
                    item["candidate_id"],
                ),
                reverse=True,
            )[:8],
            f"CONFIRMED_{scale}_RECENT",
        )

    if len(selected) > MAX_POOL_SIZE:
        # Added structural representatives have priority over inherited R1-only
        # rows.  Ties are deterministic and independent of outcomes.
        ordered = sorted(
            selected,
            key=lambda candidate_id: (
                selected[candidate_id] == {"CAMPAIGN_R1_SHORTLIST"},
                candidates[candidate_id]["observed_through"],
                candidates[candidate_id]["start_date"],
                candidate_id,
            ),
            reverse=True,
        )[:MAX_POOL_SIZE]
        selected = {candidate_id: selected[candidate_id] for candidate_id in ordered}

    pool_rows = [
        {
            "candidate_id": candidate_id,
            "selection_reasons": sorted(reasons),
        }
        for candidate_id, reasons in sorted(selected.items())
    ]
    return {
        "pool_version": POOL_VERSION,
        "status": "ALIGNMENT_CANDIDATE_POOL_ONLY（僅重現校準候選池）",
        "review_id": catalog["review_id"],
        "anonymous_stock_id": catalog["anonymous_stock_id"],
        "as_of": catalog["as_of"],
        "source_catalog_sha256": None,
        "source_packet_sha256": catalog["source_packet_sha256"],
        "generator_contract": {
            "course_judgement_included": False,
            "future_performance_used": False,
            "sealed_labels_used": False,
            "legacy_ai_answers_used": False,
            "identity_used": False,
            "selection_uses_outcomes": False,
        },
        "candidate_pool": pool_rows,
    }


def validate_pool(pool: dict[str, Any], catalog: dict[str, Any]) -> None:
    if len(pool["candidate_pool"]) > MAX_POOL_SIZE:
        raise ValueError("candidate pool exceeds maximum")
    ids = [item["candidate_id"] for item in pool["candidate_pool"]]
    if len(ids) != len(set(ids)):
        raise ValueError("candidate pool contains duplicate IDs")
    catalog_ids = {item["candidate_id"] for item in catalog["campaign_candidates"]}
    if not set(ids).issubset(catalog_ids):
        raise ValueError("candidate pool references missing catalog object")
    if pool["review_id"] != catalog["review_id"] or pool["as_of"] != catalog["as_of"]:
        raise ValueError("candidate pool source mismatch")
    if any(not item["selection_reasons"] for item in pool["candidate_pool"]):
        raise ValueError("candidate pool row lacks selection reason")


def build_all(artifact_dir: Path) -> dict[str, Any]:
    source_manifest_path = artifact_dir / "campaign_candidate_catalog_manifest_candidate_r1.json"
    source_manifest = load_json(source_manifest_path)
    output_dir = artifact_dir / OUTPUT_DIRECTORY
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for source in source_manifest["rows"]:
        catalog_path = artifact_dir / source_manifest["catalog_directory"] / source["catalog_file"]
        packet_path = artifact_dir / source_manifest["source_packet_directory"] / source["source_packet_file"]
        if sha256_path(catalog_path) != source["catalog_sha256"]:
            raise ValueError(f"catalog hash mismatch: {source['review_id']}")
        catalog = load_json(catalog_path)
        packet = load_json(packet_path)
        pool = build_pool(catalog, packet)
        pool["source_catalog_sha256"] = sha256_path(catalog_path)
        validate_pool(pool, catalog)
        output_path = output_dir / f"{pool['review_id']}.json"
        payload = canonical_bytes(pool)
        if output_path.exists() and output_path.read_bytes() != payload:
            raise FileExistsError(f"refusing to overwrite non-identical artifact: {output_path}")
        output_path.write_bytes(payload)
        rows.append(
            {
                "review_id": pool["review_id"],
                "as_of": pool["as_of"],
                "pool_file": output_path.name,
                "pool_sha256": sha256_path(output_path),
                "pool_count": len(pool["candidate_pool"]),
                "source_catalog_sha256": pool["source_catalog_sha256"],
                "source_packet_sha256": pool["source_packet_sha256"],
            }
        )
    manifest = {
        "manifest_version": POOL_VERSION,
        "status": "ALIGNMENT_CANDIDATE_POOLS_COMPLETE（重現校準候選池完成）",
        "case_count": len(rows),
        "max_pool_size": MAX_POOL_SIZE,
        "source_manifest_file": source_manifest_path.name,
        "source_manifest_sha256": sha256_path(source_manifest_path),
        "pool_directory": OUTPUT_DIRECTORY,
        "formal_ai_calls": 0,
        "future_performance_used": False,
        "sealed_labels_used": False,
        "legacy_ai_answers_used": False,
        "identity_used": False,
        "rows": sorted(rows, key=lambda item: item["review_id"]),
    }
    output_manifest = artifact_dir / "campaign_candidate_pool_manifest_candidate_r2.json"
    payload = canonical_bytes(manifest)
    if output_manifest.exists() and output_manifest.read_bytes() != payload:
        raise FileExistsError(f"refusing to overwrite non-identical artifact: {output_manifest}")
    output_manifest.write_bytes(payload)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    manifest = build_all(args.artifact_dir)
    print(json.dumps({"status": manifest["status"], "case_count": manifest["case_count"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
