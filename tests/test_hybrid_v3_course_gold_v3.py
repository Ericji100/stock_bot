from __future__ import annotations

from copy import deepcopy

import pytest

from scripts import hybrid_v3_consistency_v3 as selector
from scripts import hybrid_v3_course_gold_v3 as gold


def _contract() -> dict:
    value = selector.load_sampling_contract()
    value["status"] = "DRAFT_NOT_FINAL"
    return value


def _packet(index: int) -> dict:
    scenarios = {
        scenario: [{"hypothesis_id": f"H-{index}-{scenario}"}]
        for scenario in (
            "MATURE_TREND_PULLBACK",
            "MACRO_COPY_RESONANCE",
            "BEAR_REVERSAL_LEFT_RIGHT",
            "FRESH_Q1_EXPANSION",
        )
    }
    sufficiency = {scenario: {"status": True} for scenario in scenarios}
    evidence: list[dict] = []
    question_manifest: dict = {}
    objective = {
        "scenario_hypotheses": scenarios,
        "data_sufficiency_by_route": sufficiency,
        "parent_campaign_invalidated": True,
        "macro_defense_alert": False,
        "wait_boundary_reasons": ["MATERIAL_OBJECTIVE_HYPOTHESIS_CONFLICT"],
        "causal_cutoff_as_of": "2023-07-03",
        "performance_used_for_ordering_or_truncation": False,
        "ai_visible_evidence_sha256": selector.canonical_sha256(evidence),
    }
    packet = {
        "review_id": f"D-{index:04d}",
        "anonymous_stock_id": f"S-{index:04d}",
        "as_of": "2023-07-03",
        "question_manifest": question_manifest,
        "evidence": evidence,
        "objective_facts": objective,
        "question_manifest_sha256": selector.canonical_sha256(question_manifest),
        "evidence_catalog_sha256": selector.canonical_sha256(evidence),
    }
    packet["input_packet_sha256"] = selector.canonical_sha256(packet)
    return packet


def _records(count: int = 400) -> list[dict]:
    rows = []
    labels = list(selector.STRATA)
    for index in range(count):
        packet = _packet(index)
        rows.append(
            {
                "source_ordinal": index,
                "review_id": packet["review_id"],
                "anonymous_stock_id": packet["anonymous_stock_id"],
                "eligible_sampling_strata": labels,
                "eligible_sampling_strata_sha256": selector.canonical_sha256(labels),
                "primary_sampling_focus": labels[index % len(labels)],
                "packet_sha256": selector.canonical_sha256(packet),
                "packet": packet,
            }
        )
    return rows


def test_v3_tool_is_final_but_selection_artifact_stays_draft() -> None:
    rows = _records()
    first = gold.build_course_gold_selection_plan(rows, contract=_contract())
    second = gold.build_course_gold_selection_plan(rows, contract=_contract())
    assert gold.COURSE_GOLD_STATUS == "FINAL"
    assert first["status"] == "DRAFT_NOT_FINAL"
    assert first == second
    selected = [row for block in [first["primary"], *first["reserve_blocks"]] for row in block["rows"]]
    assert len(selected) == 360
    assert len({row["review_id"] for row in selected}) == 360
    assert all(row["sampling_focus"] in row["eligible_sampling_strata"] for row in selected)


def test_sampling_focus_is_only_in_external_annotation_manifest() -> None:
    rows = _records()
    plan = gold.build_course_gold_selection_plan(rows, contract=_contract())
    bundle = gold.build_blind_annotation_adapter(rows, plan)
    assert bundle["bundle_manifest"]["status"] == "DRAFT_UNLABELED"
    assert bundle["bundle_manifest"]["gold_labels_present"] is False
    assert bundle["annotation_manifest"]["sampling_focus_location"] == "EXTERNAL_MANIFEST_ONLY"
    external = [case for block in bundle["annotation_manifest"]["blocks"] for case in block["cases"]]
    assert all("sampling_focus" in case for case in external)
    for annotation in bundle["annotation_packets"]:
        assert not gold.SAMPLING_KEYS.intersection(gold._walk_keys(annotation["packet"]))
        assert "sampling_focus" not in annotation


def test_packet_sampling_leak_and_old_universe_are_rejected() -> None:
    rows = _records()
    plan = gold.build_course_gold_selection_plan(rows, contract=_contract())
    contaminated = deepcopy(rows)
    contaminated[0]["packet"]["sampling_focus"] = selector.STRATA[0]
    with pytest.raises(Exception, match="sampling metadata"):
        gold.build_blind_annotation_adapter(contaminated, plan)

    with pytest.raises(gold.CourseGoldV3Error, match="Candidate2"):
        gold.validate_candidate2_universe_manifest({"manifest_version": "hybrid-v3-review-points-v2"})


def test_candidate2_formal_universe_attestation() -> None:
    manifest = {
        "manifest_version": "hybrid-v3-review-points-v3",
        "status": "LOCKED_OUTCOME_BLIND",
        "universe_scope": "FULL_CANDIDATE2_REVIEW_POINT_UNIVERSE",
        "outcome_blind": True,
        "identity_visible": False,
        "performance_visible": False,
        "future_data_visible": False,
        "source_order_locked": True,
        "sampling_strata_basis": "OBJECTIVE_ONLY_AS_OF_MULTI_LABEL",
        "sampling_strata_canonical_order": list(selector.STRATA),
    }
    assert gold.validate_candidate2_universe_manifest(manifest) == manifest
