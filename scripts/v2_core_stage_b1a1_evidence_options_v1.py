"""Build objective finite evidence options for the next Stage B1a1 candidate.

The builder creates no course PASS/FAIL judgement.  It only packages existing
AS-OF facts into stable, ranked options that a later AI stage must assess one
by one.
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

from scripts.v2_core_objective_candidate_catalog_v1 import (
    ARTIFACT_DIR,
    canonical_bytes,
    sha256_path,
    write_new_or_identical,
)


CATALOG_VERSION = "v2-core-stage-b1a1-evidence-options-r1-candidate"
MANIFEST_VERSION = "v2-core-stage-b1a1-evidence-options-manifest-r1-candidate"
SOURCE_MANIFEST_FILE = "stage_b1a1_input_manifest_candidate_r2.json"
SOURCE_DIRECTORY = "stage_b1a1_input_packets_candidate_r2"
OUTPUT_DIRECTORY = "stage_b1a1_evidence_options_candidate_r1"
OUTPUT_MANIFEST_FILE = "stage_b1a1_evidence_options_manifest_candidate_r1.json"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _stable_id(payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(canonical_bytes(payload)).hexdigest()[:20]
    return f"EVO-{digest}"


def _date(value: str) -> date:
    return date.fromisoformat(value)


def _relations_for_segment(
    relations: list[dict[str, Any]], candidate_id: str
) -> list[dict[str, Any]]:
    selected = []
    for relation in relations:
        related = {
            value
            for key, value in relation.items()
            if key.endswith("_segment_id") and value is not None
        }
        if candidate_id in related:
            selected.append(relation)
    return sorted(selected, key=lambda row: str(row["candidate_id"]))


def _pivot_ref_index(packet: dict[str, Any]) -> dict[int, str]:
    result: dict[int, str] = {}
    for row in packet["evidence_catalog"]:
        path = str(row["path"])
        if row["kind"] == "PIVOT" and path.startswith("/confirmed_pivots_to_as_of/"):
            result[int(path.rsplit("/", 1)[1])] = str(row["ref"])
    return result


def _path_rows(packet: dict[str, Any], segment: dict[str, Any]) -> list[dict[str, Any]]:
    start = _date(str(segment["start_date"]))
    end = _date(str(segment["observed_through"]))
    return [
        row
        for row in packet["daily_structure_context_to_as_of"]
        if start <= _date(str(row["date"])) <= end
    ]


def _ranked_prior_targets(
    packet: dict[str, Any], segment: dict[str, Any]
) -> list[dict[str, Any]]:
    pivot_refs = _pivot_ref_index(packet)
    start = _date(str(segment["start_date"]))
    end = _date(str(segment["observed_through"]))
    target_side = "HIGH" if segment["direction"] == "UP" else "LOW"
    rows = _path_rows(packet, segment)
    observed_extreme = (
        max(float(row["high"]) for row in rows)
        if segment["direction"] == "UP"
        else min(float(row["low"]) for row in rows)
    )
    candidates = []
    for index, pivot in enumerate(packet["confirmed_pivots_to_as_of"]):
        if pivot["side"] != target_side:
            continue
        if _date(str(pivot["source_date"])) >= start:
            continue
        if _date(str(pivot["confirmation_date"])) > end:
            continue
        pivot_ref = pivot_refs.get(index)
        if pivot_ref is None:
            raise ValueError(f"missing pivot evidence ref at index {index}")
        same_scale = segment["scale"] in {"SMALL", "LARGE"} and (
            pivot["scale"] == segment["scale"]
        )
        reached = (
            observed_extreme >= float(pivot["price"])
            if segment["direction"] == "UP"
            else observed_extreme <= float(pivot["price"])
        )
        candidates.append(
            {
                "target_pivot_ref": pivot_ref,
                "target_source_date": pivot["source_date"],
                "target_confirmation_date": pivot["confirmation_date"],
                "target_price": pivot["price"],
                "target_scale": pivot["scale"],
                "candidate_scale": segment["scale"],
                "same_scale_objective": same_scale,
                "observed_path_extreme": round(observed_extreme, 6),
                "price_reached_or_crossed_objective": reached,
            }
        )
    candidates.sort(
        key=lambda row: (
            not row["same_scale_objective"],
            -_date(str(row["target_source_date"])).toordinal(),
            str(row["target_pivot_ref"]),
        )
    )
    same_scale = [row for row in candidates if row["same_scale_objective"]][:6]
    other_scale = [row for row in candidates if not row["same_scale_objective"]][:3]
    return [*same_scale, *other_scale]


def build_evidence_option_catalog(packet: dict[str, Any], packet_sha256: str) -> dict[str, Any]:
    evidence_refs = {str(row["ref"]) for row in packet["evidence_catalog"]}
    options: list[dict[str, Any]] = []
    per_candidate: list[dict[str, Any]] = []

    for segment in sorted(packet["focus_segments"], key=lambda row: row["candidate_id"]):
        candidate_id = str(segment["candidate_id"])
        candidate_option_ids: list[str] = []

        directional_payload = {
            "candidate_id": candidate_id,
            "atom_name": "directional_coherence",
            "option_kind": "COMPLETE_PATH_BUNDLE",
            "evidence_rank": 1,
            "source_evidence_refs": list(segment["source_evidence_refs"]),
            "path": {
                "start_date": segment["start_date"],
                "observed_through": segment["observed_through"],
                "direction": segment["direction"],
                "scale": segment["scale"],
                "status": segment["status"],
                "objective_metrics": segment["objective_metrics"],
            },
        }
        directional = {
            **directional_payload,
            "evidence_option_id": _stable_id(directional_payload),
            "not_course_judgement": True,
        }
        options.append(directional)
        candidate_option_ids.append(directional["evidence_option_id"])

        for rank, target in enumerate(_ranked_prior_targets(packet, segment), start=1):
            payload = {
                "candidate_id": candidate_id,
                "atom_name": "structural_challenge_or_break",
                "option_kind": "FIXED_PRIOR_PIVOT_TARGET",
                "evidence_rank": rank,
                "source_evidence_refs": [target["target_pivot_ref"]],
                "target": target,
            }
            option = {
                **payload,
                "evidence_option_id": _stable_id(payload),
                "not_course_judgement": True,
            }
            options.append(option)
            candidate_option_ids.append(option["evidence_option_id"])

        invalidation_refs = [
            ref
            for ref in segment["source_evidence_refs"]
            if str(ref).startswith(("PIVOT:", "PRICE:"))
        ]
        invalidation_refs.sort(
            key=lambda ref: (0 if str(ref).startswith("PIVOT:") else 1, str(ref))
        )
        for rank, ref in enumerate(invalidation_refs, start=1):
            payload = {
                "candidate_id": candidate_id,
                "atom_name": "invalidation_traceability",
                "option_kind": "FIXED_INVALIDATION_REFERENCE",
                "evidence_rank": rank,
                "source_evidence_refs": [ref],
                "reference": {"ref": ref},
            }
            option = {
                **payload,
                "evidence_option_id": _stable_id(payload),
                "not_course_judgement": True,
            }
            options.append(option)
            candidate_option_ids.append(option["evidence_option_id"])

        relations = _relations_for_segment(packet["focus_relations"], candidate_id)
        for rank, relation in enumerate(relations, start=1):
            ref = str(relation["candidate_id"])
            payload = {
                "candidate_id": candidate_id,
                "atom_name": "as_of_relation_link",
                "option_kind": "FIXED_RELATION",
                "evidence_rank": rank,
                "source_evidence_refs": [ref],
                "relation": relation,
            }
            option = {
                **payload,
                "evidence_option_id": _stable_id(payload),
                "not_course_judgement": True,
            }
            options.append(option)
            candidate_option_ids.append(option["evidence_option_id"])

        per_candidate.append(
            {
                "candidate_id": candidate_id,
                "evidence_option_ids": candidate_option_ids,
            }
        )

    option_ids = [row["evidence_option_id"] for row in options]
    if len(option_ids) != len(set(option_ids)):
        raise ValueError(f"duplicate evidence option id for {packet['review_id']}")
    for option in options:
        unknown = set(option["source_evidence_refs"]) - evidence_refs
        if unknown:
            raise ValueError(f"unknown evidence refs {sorted(unknown)}")

    return {
        "catalog_version": CATALOG_VERSION,
        "status": "CANDIDATE_OBJECTIVE_OPTIONS_ONLY（候選客觀選項、尚非課程判讀）",
        "review_id": packet["review_id"],
        "anonymous_stock_id": packet["anonymous_stock_id"],
        "as_of": packet["as_of"],
        "source_packet_sha256": packet_sha256,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "course_judgement_generated_by_program": False,
        "evidence_options": options,
        "candidate_option_index": per_candidate,
    }


def generate_all(artifact_dir: Path = ARTIFACT_DIR) -> dict[str, Any]:
    source_manifest_path = artifact_dir / SOURCE_MANIFEST_FILE
    source_manifest = load_json(source_manifest_path)
    output_dir = artifact_dir / OUTPUT_DIRECTORY
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    atom_counts: dict[str, int] = {}
    total_options = 0

    for source_row in source_manifest["rows"]:
        source_path = artifact_dir / SOURCE_DIRECTORY / source_row["input_packet_file"]
        source_hash = sha256_path(source_path)
        if source_hash != source_row["input_packet_sha256"]:
            raise ValueError(f"source packet hash mismatch for {source_row['review_id']}")
        packet = load_json(source_path)
        catalog = build_evidence_option_catalog(packet, source_hash)
        output_path = output_dir / f"{packet['review_id']}.json"
        write_new_or_identical(output_path, canonical_bytes(catalog))
        counts: dict[str, int] = {}
        for option in catalog["evidence_options"]:
            atom = str(option["atom_name"])
            counts[atom] = counts.get(atom, 0) + 1
            atom_counts[atom] = atom_counts.get(atom, 0) + 1
        total_options += len(catalog["evidence_options"])
        rows.append(
            {
                "review_id": packet["review_id"],
                "anonymous_stock_id": packet["anonymous_stock_id"],
                "as_of": packet["as_of"],
                "catalog_file": output_path.name,
                "catalog_sha256": sha256_path(output_path),
                "source_packet_file": source_path.name,
                "source_packet_sha256": source_hash,
                "candidate_count": len(packet["focus_segments"]),
                "evidence_option_count": len(catalog["evidence_options"]),
                "atom_option_counts": counts,
            }
        )

    rows.sort(key=lambda row: row["review_id"])
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "status": "DESIGN_INPUT_COMPLETE_NOT_READY_FOR_AI（設計輸入完成、尚不可執行AI）",
        "case_count": len(rows),
        "formal_ai_calls": 0,
        "ready_for_formal_ai": False,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "course_judgement_generated_by_program": False,
        "source_manifest_file": SOURCE_MANIFEST_FILE,
        "source_manifest_sha256": sha256_path(source_manifest_path),
        "catalog_directory": OUTPUT_DIRECTORY,
        "total_evidence_options": total_options,
        "atom_option_counts": dict(sorted(atom_counts.items())),
        "rows": rows,
    }
    manifest_path = artifact_dir / OUTPUT_MANIFEST_FILE
    write_new_or_identical(manifest_path, canonical_bytes(manifest))
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    return parser.parse_args()


def main() -> int:
    manifest = generate_all(parse_args().artifact_dir)
    print(
        json.dumps(
            {
                key: manifest[key]
                for key in (
                    "status",
                    "case_count",
                    "formal_ai_calls",
                    "ready_for_formal_ai",
                    "total_evidence_options",
                    "atom_option_counts",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
