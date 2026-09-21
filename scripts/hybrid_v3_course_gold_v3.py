"""Candidate2 multi-label course-gold selection and blind packet adapter.

This FINAL tool component does not create human rubrics, call AI, or evaluate
performance.  Its generated selection and annotation artifacts remain DRAFT
and UNLABELED until the separate human lifecycle is complete.  It consumes
only the locked Candidate2 outcome-blind source,
delegates allocation to the canonical fixed-seed multi-label max-flow
selector, and keeps ``sampling_focus`` outside every AI-visible packet.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import json
from typing import Any, Mapping, Sequence

try:
    from .hybrid_v3_consistency_v3 import (
        SELECTOR_VERSION,
        STRATA,
        build_multilabel_holdout_plan,
        canonical_sha256,
        load_sampling_contract,
        validate_source_records,
    )
except ImportError:
    from hybrid_v3_consistency_v3 import (
        SELECTOR_VERSION,
        STRATA,
        build_multilabel_holdout_plan,
        canonical_sha256,
        load_sampling_contract,
        validate_source_records,
    )


COURSE_GOLD_VERSION = "hybrid-v3-course-gold-v3-multilabel"
COURSE_GOLD_STATUS = "FINAL"
SELECTION_PLAN_VERSION = "hybrid-v3-course-gold-selection-plan-v3"
ANNOTATION_PACKET_VERSION = "hybrid-v3-course-gold-annotation-packet-v3"
ANNOTATION_MANIFEST_VERSION = "hybrid-v3-course-gold-annotation-manifest-v3"
BUNDLE_MANIFEST_VERSION = "hybrid-v3-course-gold-bundle-v3"
FORMAL_UNIVERSE_VERSION = "hybrid-v3-review-points-v3"
FORMAL_UNIVERSE_SCOPE = "FULL_CANDIDATE2_REVIEW_POINT_UNIVERSE"

SAMPLING_KEYS = frozenset(
    {"eligible_sampling_strata", "primary_sampling_focus", "sampling_stratum", "sampling_focus"}
)
FORBIDDEN_AI_PACKET_KEYS = frozenset(
    {
        "code",
        "name",
        "symbol",
        "ticker",
        "company_name",
        "identity_map",
        "ai_output",
        "prior_ai_output",
        "outcome",
        "mfe",
        "mae",
        "pnl",
        "profit",
        "return_after",
        "forward_return",
        "future_return",
        "exit_date",
        "exit_price",
    }
)
ALLOWED_PACKET_KEYS = frozenset(
    {
        "review_id",
        "anonymous_stock_id",
        "as_of",
        "question_manifest",
        "evidence",
        "objective_facts",
        "question_manifest_sha256",
        "evidence_catalog_sha256",
        "input_packet_sha256",
    }
)


class CourseGoldV3Error(ValueError):
    """Candidate2 course-gold input or blind adapter contract is invalid."""


def _walk_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, Mapping):
        for key, nested in value.items():
            keys.add(str(key))
            keys.update(_walk_keys(nested))
    elif isinstance(value, list):
        for nested in value:
            keys.update(_walk_keys(nested))
    return keys


def _packet_integrity(packet: Mapping[str, Any]) -> dict[str, Any]:
    value = deepcopy(dict(packet))
    if SAMPLING_KEYS.intersection(_walk_keys(value)):
        raise CourseGoldV3Error("sampling metadata must not enter an AI-visible packet")
    keys = {key.lower() for key in _walk_keys(value)}
    if FORBIDDEN_AI_PACKET_KEYS.intersection(keys) or any(
        key.startswith(("future_", "forward_", "prior_ai_")) for key in keys
    ):
        raise CourseGoldV3Error("identity, prior-AI, or outcome data entered an AI-visible packet")
    if set(value) != ALLOWED_PACKET_KEYS:
        raise CourseGoldV3Error("Candidate2 packet top-level fields are not exact")
    if value.get("question_manifest_sha256") != canonical_sha256(value.get("question_manifest")):
        raise CourseGoldV3Error("question manifest hash mismatch")
    if value.get("evidence_catalog_sha256") != canonical_sha256(value.get("evidence")):
        raise CourseGoldV3Error("evidence catalog hash mismatch")
    objective = value.get("objective_facts")
    if not isinstance(objective, Mapping):
        raise CourseGoldV3Error("objective_facts must be an object")
    if objective.get("ai_visible_evidence_sha256") != canonical_sha256(value.get("evidence")):
        raise CourseGoldV3Error("objective evidence hash mismatch")
    as_of = str(value.get("as_of") or "")
    try:
        cutoff = date.fromisoformat(as_of)
    except ValueError as exc:
        raise CourseGoldV3Error("packet as_of must be an ISO date") from exc
    if objective.get("causal_cutoff_as_of") != as_of:
        raise CourseGoldV3Error("objective cutoff differs from packet as_of")
    if objective.get("performance_used_for_ordering_or_truncation") is not False:
        raise CourseGoldV3Error("packet lacks the outcome-blind ordering attestation")
    for evidence in value.get("evidence") or []:
        try:
            evidence_date = date.fromisoformat(str(evidence.get("date")))
        except (AttributeError, ValueError) as exc:
            raise CourseGoldV3Error("evidence needs an ISO date") from exc
        if evidence_date > cutoff:
            raise CourseGoldV3Error("future evidence entered an AI-visible packet")
    core = dict(value)
    supplied = core.pop("input_packet_sha256", None)
    if supplied != canonical_sha256(core):
        raise CourseGoldV3Error("input packet hash mismatch")
    return value


def validate_candidate2_universe_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(manifest)
    if value.get("manifest_version") != FORMAL_UNIVERSE_VERSION:
        raise CourseGoldV3Error("course-gold v3 requires the Candidate2 review-point manifest")
    if value.get("status") != "LOCKED_OUTCOME_BLIND":
        raise CourseGoldV3Error("Candidate2 review-point universe is not locked")
    if value.get("universe_scope") != FORMAL_UNIVERSE_SCOPE:
        raise CourseGoldV3Error("course-gold v3 requires the full Candidate2 universe")
    required = {
        "outcome_blind": True,
        "identity_visible": False,
        "performance_visible": False,
        "future_data_visible": False,
        "source_order_locked": True,
    }
    for key, expected in required.items():
        if value.get(key) is not expected:
            raise CourseGoldV3Error(f"Candidate2 universe violates {key}={expected!r}")
    if value.get("sampling_strata_basis") != "OBJECTIVE_ONLY_AS_OF_MULTI_LABEL":
        raise CourseGoldV3Error("Candidate2 sampling basis is not objective-only multi-label")
    if value.get("sampling_strata_canonical_order") != list(STRATA):
        raise CourseGoldV3Error("Candidate2 sampling label order changed")
    return value


def build_course_gold_selection_plan(
    records: Sequence[Mapping[str, Any]],
    *,
    contract: Mapping[str, Any] | None = None,
    excluded_review_ids: Sequence[str] = (),
    excluded_anonymous_stock_ids: Sequence[str] = (),
    source_artifact_sha256: str | None = None,
    source_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Select globally disjoint primary/reserves with the canonical allocator."""
    contract_value = deepcopy(dict(contract)) if contract is not None else load_sampling_contract()
    normalized = validate_source_records(records, contract_value)
    review_exclusions = set(excluded_review_ids)
    stock_exclusions = set(excluded_anonymous_stock_ids)
    eligible = [
        row
        for row in normalized
        if row["review_id"] not in review_exclusions
        and row["anonymous_stock_id"] not in stock_exclusions
    ]
    if not eligible:
        raise CourseGoldV3Error("course-gold exclusions removed every Candidate2 case")
    allocation = build_multilabel_holdout_plan(
        eligible,
        contract=contract_value,
        source_artifact_sha256=source_artifact_sha256,
        source_manifest_sha256=source_manifest_sha256,
    )
    core = {
        "plan_version": SELECTION_PLAN_VERSION,
        "status": "DRAFT_NOT_FINAL",
        "course_gold_version": COURSE_GOLD_VERSION,
        "course_gold_status": COURSE_GOLD_STATUS,
        "selector_version": SELECTOR_VERSION,
        "allocation_plan_sha256": allocation["plan_sha256"],
        "contract_version": allocation["contract_version"],
        "contract_sha256": allocation["contract_sha256"],
        "seed": allocation["seed"],
        "allocation_policy": allocation["allocation_policy"],
        "source_artifact_sha256": source_artifact_sha256,
        "source_manifest_sha256": source_manifest_sha256,
        "excluded_review_ids_sha256": canonical_sha256(sorted(review_exclusions)),
        "excluded_anonymous_stock_ids_sha256": canonical_sha256(sorted(stock_exclusions)),
        "sampling_focus_is_external_annotation_metadata_only": True,
        "primary": allocation["primary"],
        "reserve_blocks": allocation["reserve_blocks"],
    }
    return {**core, "selection_plan_sha256": canonical_sha256(core)}


def _gold_case_id(seed: str, review_id: str, packet_sha256: str) -> str:
    material = f"{seed}|{review_id}|{packet_sha256}|CANDIDATE2_COURSE_GOLD_V3"
    return "CG3-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def build_blind_annotation_adapter(
    records: Sequence[Mapping[str, Any]], selection_plan: Mapping[str, Any]
) -> dict[str, Any]:
    """Adapt selected cases without exposing the selection focus to the AI."""
    plan = dict(selection_plan)
    supplied = plan.pop("selection_plan_sha256", None)
    if supplied != canonical_sha256(plan) or plan.get("plan_version") != SELECTION_PLAN_VERSION:
        raise CourseGoldV3Error("selection plan hash/version mismatch")
    by_review_id = {str(row["review_id"]): row for row in records}
    annotations: list[dict[str, Any]] = []
    manifest_blocks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for block in [selection_plan["primary"], *selection_plan["reserve_blocks"]]:
        external_cases: list[dict[str, Any]] = []
        for selected in block["rows"]:
            review_id = str(selected["review_id"])
            if review_id in seen or review_id not in by_review_id:
                raise CourseGoldV3Error("selection contains a duplicate or missing review_id")
            seen.add(review_id)
            source = by_review_id[review_id]
            packet = _packet_integrity(source["packet"])
            if canonical_sha256(packet) != selected["packet_sha256"]:
                raise CourseGoldV3Error("selected packet differs from Candidate2 source")
            case_id = _gold_case_id(str(selection_plan["seed"]), review_id, selected["packet_sha256"])
            annotation = {
                "annotation_packet_version": ANNOTATION_PACKET_VERSION,
                "gold_case_id": case_id,
                "packet": packet,
                "annotation_packet_sha256": canonical_sha256(packet),
            }
            if SAMPLING_KEYS.intersection(_walk_keys(annotation["packet"])):
                raise CourseGoldV3Error("sampling metadata leaked into annotation packet")
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
    annotation_manifest_core = {
        "manifest_version": ANNOTATION_MANIFEST_VERSION,
        "status": "DRAFT_UNLABELED",
        "gold_labels_present": False,
        "selection_plan_sha256": supplied,
        "sampling_focus_location": "EXTERNAL_MANIFEST_ONLY",
        "ai_packet_sampling_metadata_visible": False,
        "blocks": manifest_blocks,
        "annotation_packets_sha256": canonical_sha256(annotations),
    }
    annotation_manifest = {
        **annotation_manifest_core,
        "annotation_manifest_sha256": canonical_sha256(annotation_manifest_core),
    }
    bundle_core = {
        "manifest_version": BUNDLE_MANIFEST_VERSION,
        "status": "DRAFT_UNLABELED",
        "gold_labels_present": False,
        "selection_plan_sha256": supplied,
        "annotation_manifest_sha256": annotation_manifest["annotation_manifest_sha256"],
        "annotation_packets_sha256": canonical_sha256(annotations),
    }
    return {
        "selection_plan": dict(selection_plan),
        "annotation_packets": annotations,
        "annotation_manifest": annotation_manifest,
        "bundle_manifest": {**bundle_core, "bundle_manifest_sha256": canonical_sha256(bundle_core)},
    }


__all__ = [
    "ANNOTATION_MANIFEST_VERSION",
    "ANNOTATION_PACKET_VERSION",
    "BUNDLE_MANIFEST_VERSION",
    "COURSE_GOLD_STATUS",
    "COURSE_GOLD_VERSION",
    "CourseGoldV3Error",
    "build_blind_annotation_adapter",
    "build_course_gold_selection_plan",
    "validate_candidate2_universe_manifest",
]
