from __future__ import annotations

import json

from scripts import v2_core_campaign_candidate_pool_v2 as module


def _load(review_id: str) -> tuple[dict, dict]:
    artifact = module.ARTIFACT_DIR
    catalog = json.loads(
        (artifact / "campaign_candidate_catalogs_candidate_r1" / f"{review_id}.json").read_text(encoding="utf-8")
    )
    packet = json.loads(
        (artifact / "feasibility_probe_packets_r2" / f"{review_id}.json").read_text(encoding="utf-8")
    )
    return catalog, packet


def test_pool_is_deterministic_bounded_and_outcome_blind() -> None:
    catalog, packet = _load("FP-a956dbdf5f936dfdb64d45ec")
    first = module.build_pool(catalog, packet)
    second = module.build_pool(catalog, packet)
    assert module.canonical_bytes(first) == module.canonical_bytes(second)
    assert len(first["candidate_pool"]) <= module.MAX_POOL_SIZE
    assert first["generator_contract"] == {
        "course_judgement_included": False,
        "future_performance_used": False,
        "sealed_labels_used": False,
        "legacy_ai_answers_used": False,
        "identity_used": False,
        "selection_uses_outcomes": False,
    }
    serialized = module.canonical_bytes(first).decode("utf-8").lower()
    for forbidden in ("legacy_v2_trigger", "case_role", "future_return", "mfe", "mae", "stock_code"):
        assert forbidden not in serialized


def test_recent_large_endpoint_group_retains_cross_scale_campaign() -> None:
    catalog, packet = _load("FP-a956dbdf5f936dfdb64d45ec")
    pool = module.build_pool(catalog, packet)
    pool_ids = {item["candidate_id"] for item in pool["candidate_pool"]}
    matches = [
        item
        for item in catalog["campaign_candidates"]
        if item["direction"] == "UP"
        and item["start_date"] == "2023-06-08"
        and item["confirmed_end_date"] == "2023-07-27"
    ]
    assert matches
    assert any(item["candidate_id"] in pool_ids for item in matches)


def test_scale_balancing_retains_forming_large_campaign() -> None:
    catalog, packet = _load("FP-7a92f125f3c66ad4e26d7943")
    pool = module.build_pool(catalog, packet)
    pool_ids = {item["candidate_id"] for item in pool["candidate_pool"]}
    matches = [
        item
        for item in catalog["campaign_candidates"]
        if item["direction"] == "UP"
        and item["start_date"] == "2023-03-16"
        and item["confirmed_end_date"] is None
    ]
    assert matches
    assert any(item["candidate_id"] in pool_ids for item in matches)
