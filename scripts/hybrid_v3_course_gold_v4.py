"""Candidate3 blind course-gold bundle and human-review subset builder.

This tool never labels cases, calls a model, resolves stock identity, or reads
future/performance data.  It converts the already locked Candidate3 holdout
allocation into blind annotation packets and a small, deterministic,
human-review plan.  All generated artifacts remain explicitly unlabeled until
the user completes and freezes the separate rubric lifecycle.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from .hybrid_v3_consistency_v3 import STRATA, canonical_sha256
    from .hybrid_v3_course_gold_v3 import (
        CourseGoldV3Error,
        SAMPLING_KEYS,
        _packet_integrity,
        _walk_keys,
    )
except ImportError:
    from hybrid_v3_consistency_v3 import STRATA, canonical_sha256
    from hybrid_v3_course_gold_v3 import (
        CourseGoldV3Error,
        SAMPLING_KEYS,
        _packet_integrity,
        _walk_keys,
    )


COURSE_GOLD_VERSION = "hybrid-v3-course-gold-v4-candidate3"
COURSE_GOLD_STATUS = "FINAL"
ANNOTATION_PACKET_VERSION = "hybrid-v3-course-gold-annotation-packet-v4"
ANNOTATION_MANIFEST_VERSION = "hybrid-v3-course-gold-annotation-manifest-v4"
HUMAN_REVIEW_PLAN_VERSION = "hybrid-v3-course-gold-human-review-plan-v4"
BUNDLE_MANIFEST_VERSION = "hybrid-v3-course-gold-bundle-v4"
FORMAL_UNIVERSE_VERSION = "hybrid-v3-review-points-v4"
FORMAL_UNIVERSE_SCOPE = "FULL_CANDIDATE3_REVIEW_POINT_UNIVERSE"
HOLDOUT_PLAN_VERSION = "hybrid-v3-multilabel-holdout-plan-v3"


class CourseGoldV4Error(CourseGoldV3Error):
    """Candidate3 course-gold source or lifecycle contract is invalid."""


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise CourseGoldV4Error(f"expected JSON object: {path}")
    return value


def _jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CourseGoldV4Error(f"invalid JSONL at {path}:{line_number}") from exc
            if not isinstance(value, dict):
                raise CourseGoldV4Error(f"expected object at {path}:{line_number}")
            yield value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def validate_candidate3_universe_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(manifest)
    if value.get("manifest_version") != FORMAL_UNIVERSE_VERSION:
        raise CourseGoldV4Error("course-gold v4 requires the Candidate3 review-point manifest")
    if value.get("status") != "LOCKED_OUTCOME_BLIND":
        raise CourseGoldV4Error("Candidate3 review-point universe is not locked")
    if value.get("universe_scope") != FORMAL_UNIVERSE_SCOPE:
        raise CourseGoldV4Error("course-gold v4 requires the full Candidate3 universe")
    required = {
        "outcome_blind": True,
        "identity_visible": False,
        "performance_visible": False,
        "future_data_visible": False,
        "source_order_locked": True,
    }
    for key, expected in required.items():
        if value.get(key) is not expected:
            raise CourseGoldV4Error(f"Candidate3 universe violates {key}={expected!r}")
    if value.get("sampling_strata_basis") != "OBJECTIVE_ONLY_AS_OF_MULTI_LABEL":
        raise CourseGoldV4Error("Candidate3 sampling basis is not objective-only multi-label")
    if value.get("sampling_strata_canonical_order") != list(STRATA):
        raise CourseGoldV4Error("Candidate3 sampling label order changed")
    return value


def validate_holdout_plan(
    plan: Mapping[str, Any],
    *,
    source_manifest: Mapping[str, Any],
    source_path: Path,
    source_manifest_path: Path,
) -> dict[str, Any]:
    value = deepcopy(dict(plan))
    supplied = value.pop("plan_sha256", None)
    if supplied != canonical_sha256(value):
        raise CourseGoldV4Error("holdout plan hash mismatch")
    if value.get("plan_version") != HOLDOUT_PLAN_VERSION or value.get("status") != "LOCKED":
        raise CourseGoldV4Error("course-gold v4 requires a locked v3 holdout plan")
    if value.get("block_count") != 3 or value.get("cases_per_block") != 120:
        raise CourseGoldV4Error("Candidate3 holdout must contain three 120-case blocks")
    if value.get("source_artifact_sha256") != _file_sha256(source_path):
        raise CourseGoldV4Error("holdout source artifact hash mismatch")
    if value.get("source_manifest_sha256") != _file_sha256(source_manifest_path):
        raise CourseGoldV4Error("holdout source manifest hash mismatch")
    if value.get("source_artifact_sha256") != source_manifest.get("artifact_sha256"):
        raise CourseGoldV4Error("holdout source differs from Candidate3 manifest")
    blocks = [value["primary"], *value["reserve_blocks"]]
    review_ids = [row["review_id"] for block in blocks for row in block["rows"]]
    if len(review_ids) != 360 or len(set(review_ids)) != 360:
        raise CourseGoldV4Error("Candidate3 holdout cases must be globally disjoint")
    return dict(plan)


def _gold_case_id(seed: str, review_id: str, packet_sha256: str) -> str:
    material = f"{seed}|{review_id}|{packet_sha256}|CANDIDATE3_COURSE_GOLD_V4"
    return "CG4-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def extract_selected_records(
    source_path: Path, selection_plan: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    blocks = [selection_plan["primary"], *selection_plan["reserve_blocks"]]
    selected = {str(row["review_id"]): row for block in blocks for row in block["rows"]}
    found: dict[str, dict[str, Any]] = {}
    for row in _jsonl(source_path):
        review_id = str(row.get("review_id") or "")
        if review_id not in selected:
            continue
        expected = selected[review_id]
        if row.get("anonymous_stock_id") != expected.get("anonymous_stock_id"):
            raise CourseGoldV4Error("selected anonymous stock id differs from source")
        packet = _packet_integrity(row.get("packet") or {})
        if canonical_sha256(packet) != expected.get("packet_sha256"):
            raise CourseGoldV4Error("selected packet differs from Candidate3 source")
        if row.get("eligible_sampling_strata") != expected.get("eligible_sampling_strata"):
            raise CourseGoldV4Error("selected sampling labels differ from source")
        found[review_id] = row
    missing = sorted(set(selected) - set(found))
    if missing:
        raise CourseGoldV4Error(f"selected Candidate3 cases missing from source: {missing[:5]}")
    return found


def build_blind_annotation_bundle(
    selected_records: Mapping[str, Mapping[str, Any]], selection_plan: Mapping[str, Any]
) -> dict[str, Any]:
    annotations: list[dict[str, Any]] = []
    manifest_blocks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for block in [selection_plan["primary"], *selection_plan["reserve_blocks"]]:
        external_cases: list[dict[str, Any]] = []
        for selected in block["rows"]:
            review_id = str(selected["review_id"])
            if review_id in seen:
                raise CourseGoldV4Error("duplicate review id in Candidate3 holdout")
            seen.add(review_id)
            source = selected_records[review_id]
            packet = _packet_integrity(source["packet"])
            case_id = _gold_case_id(str(selection_plan["seed"]), review_id, selected["packet_sha256"])
            annotation = {
                "annotation_packet_version": ANNOTATION_PACKET_VERSION,
                "gold_case_id": case_id,
                "packet": packet,
                "annotation_packet_sha256": canonical_sha256(packet),
            }
            if SAMPLING_KEYS.intersection(_walk_keys(annotation["packet"])):
                raise CourseGoldV4Error("sampling metadata leaked into AI-visible annotation packet")
            annotations.append(annotation)
            external_cases.append(
                {
                    "gold_case_id": case_id,
                    "review_id": review_id,
                    "anonymous_stock_id": selected["anonymous_stock_id"],
                    "sampling_focus": selected["sampling_focus"],
                    "eligible_sampling_strata": selected["eligible_sampling_strata"],
                    "eligible_sampling_strata_sha256": selected["eligible_sampling_strata_sha256"],
                    "packet_sha256": selected["packet_sha256"],
                }
            )
        manifest_blocks.append(
            {
                "block_id": block["block_id"],
                "count": len(external_cases),
                "cases": external_cases,
                "cases_sha256": canonical_sha256(external_cases),
            }
        )
    manifest_core = {
        "manifest_version": ANNOTATION_MANIFEST_VERSION,
        "status": "DRAFT_UNLABELED",
        "gold_labels_present": False,
        "selection_plan_sha256": selection_plan["plan_sha256"],
        "sampling_focus_location": "EXTERNAL_MANIFEST_ONLY",
        "ai_packet_sampling_metadata_visible": False,
        "blocks": manifest_blocks,
        "annotation_packets_sha256": canonical_sha256(annotations),
    }
    return {
        "annotation_packets": annotations,
        "annotation_manifest": {
            **manifest_core,
            "annotation_manifest_sha256": canonical_sha256(manifest_core),
        },
    }


def build_human_review_plan(
    annotation_manifest: Mapping[str, Any], *, cases_per_stratum: int = 4
) -> dict[str, Any]:
    if cases_per_stratum < 1:
        raise CourseGoldV4Error("cases_per_stratum must be positive")
    primary_cases = list(annotation_manifest["blocks"][0]["cases"])
    selected: list[dict[str, Any]] = []
    used_stocks: set[str] = set()
    used_cases: set[str] = set()
    for stratum in STRATA:
        candidates = [case for case in primary_cases if case["sampling_focus"] == stratum]
        chosen = [case for case in candidates if case["anonymous_stock_id"] not in used_stocks][
            :cases_per_stratum
        ]
        if len(chosen) < cases_per_stratum:
            chosen_ids = {case["gold_case_id"] for case in chosen}
            chosen.extend(
                case
                for case in candidates
                if case["gold_case_id"] not in chosen_ids and case["gold_case_id"] not in used_cases
            )
            chosen = chosen[:cases_per_stratum]
        if len(chosen) != cases_per_stratum:
            raise CourseGoldV4Error(f"not enough human-review cases for {stratum}")
        for case in chosen:
            selected.append(
                {
                    "gold_case_id": case["gold_case_id"],
                    "sampling_focus": stratum,
                    "anonymous_stock_id": case["anonymous_stock_id"],
                    "review_id": case["review_id"],
                    "packet_sha256": case["packet_sha256"],
                }
            )
            used_stocks.add(case["anonymous_stock_id"])
            used_cases.add(case["gold_case_id"])
    core = {
        "plan_version": HUMAN_REVIEW_PLAN_VERSION,
        "status": "DRAFT_AWAITING_HUMAN_LABELS",
        "gold_labels_present": False,
        "outcome_blind": True,
        "identity_visible_to_human": False,
        "future_visible_to_human": False,
        "old_ai_output_visible_to_human": False,
        "cases_per_stratum": cases_per_stratum,
        "distinct_strata": len(STRATA),
        "case_count": len(selected),
        "cases": selected,
        "cases_sha256": canonical_sha256(selected),
    }
    return {**core, "human_review_plan_sha256": canonical_sha256(core)}


def write_bundle(
    *,
    source_path: Path,
    source_manifest_path: Path,
    holdout_plan_path: Path,
    output_dir: Path,
    cases_per_stratum: int = 4,
) -> dict[str, Any]:
    source_manifest = validate_candidate3_universe_manifest(_read_json(source_manifest_path))
    selection_plan = validate_holdout_plan(
        _read_json(holdout_plan_path),
        source_manifest=source_manifest,
        source_path=source_path,
        source_manifest_path=source_manifest_path,
    )
    selected_records = extract_selected_records(source_path, selection_plan)
    bundle = build_blind_annotation_bundle(selected_records, selection_plan)
    human_review_plan = build_human_review_plan(
        bundle["annotation_manifest"], cases_per_stratum=cases_per_stratum
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    selection_path = output_dir / "selection_plan.json"
    annotation_packets_path = output_dir / "annotation_packets.jsonl"
    annotation_manifest_path = output_dir / "annotation_manifest.json"
    human_review_plan_path = output_dir / "human_review_plan.json"
    _write_json(selection_path, selection_plan)
    _write_jsonl(annotation_packets_path, bundle["annotation_packets"])
    _write_json(annotation_manifest_path, bundle["annotation_manifest"])
    _write_json(human_review_plan_path, human_review_plan)

    files = {
        path.name: {"sha256": _file_sha256(path), "bytes": path.stat().st_size}
        for path in (
            selection_path,
            annotation_packets_path,
            annotation_manifest_path,
            human_review_plan_path,
        )
    }
    bundle_core = {
        "manifest_version": BUNDLE_MANIFEST_VERSION,
        "status": "DRAFT_UNLABELED",
        "course_gold_version": COURSE_GOLD_VERSION,
        "course_gold_status": COURSE_GOLD_STATUS,
        "gold_labels_present": False,
        "outcome_blind": True,
        "identity_visible": False,
        "future_visible": False,
        "old_ai_output_visible": False,
        "source_artifact_sha256": source_manifest["artifact_sha256"],
        "source_manifest_sha256": _file_sha256(source_manifest_path),
        "selection_plan_sha256": selection_plan["plan_sha256"],
        "annotation_manifest_sha256": bundle["annotation_manifest"][
            "annotation_manifest_sha256"
        ],
        "human_review_plan_sha256": human_review_plan["human_review_plan_sha256"],
        "annotation_case_count": len(bundle["annotation_packets"]),
        "human_review_case_count": human_review_plan["case_count"],
        "files": files,
    }
    bundle_manifest = {
        **bundle_core,
        "bundle_manifest_sha256": canonical_sha256(bundle_core),
    }
    _write_json(output_dir / "bundle_manifest.json", bundle_manifest)
    return bundle_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--holdout-plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cases-per-stratum", type=int, default=4)
    args = parser.parse_args()
    manifest = write_bundle(
        source_path=args.source.resolve(),
        source_manifest_path=args.source_manifest.resolve(),
        holdout_plan_path=args.holdout_plan.resolve(),
        output_dir=args.output_dir.resolve(),
        cases_per_stratum=args.cases_per_stratum,
    )
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "annotation_case_count": manifest["annotation_case_count"],
                "human_review_case_count": manifest["human_review_case_count"],
                "bundle_manifest_sha256": manifest["bundle_manifest_sha256"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
