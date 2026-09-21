from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import v2_core_campaign_candidate_catalog_v1 as module


ARTIFACT_DIR = module.ARTIFACT_DIR


def _load_packet(review_id: str) -> dict:
    path = ARTIFACT_DIR / module.SOURCE_PACKET_DIRECTORY / f"{review_id}.json"
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _schema() -> dict:
    return json.loads((ARTIFACT_DIR / module.SCHEMA_FILE).read_text(encoding="utf-8-sig"))


def test_campaign_catalog_is_deterministic_and_outcome_blind() -> None:
    packet = _load_packet("FP-46f54e49cda2a16d69f9c6d6")
    packet_bytes = (ARTIFACT_DIR / module.SOURCE_PACKET_DIRECTORY / f"{packet['review_id']}.json").read_bytes()
    packet_sha = hashlib.sha256(packet_bytes).hexdigest()
    first = module.build_catalog(packet, packet_sha, _schema())
    second = module.build_catalog(packet, packet_sha, _schema())
    assert module.canonical_bytes(first) == module.canonical_bytes(second)
    assert first["generator_contract"] == {
        "candidate_only": True,
        "ai_course_judgement_included": False,
        "future_performance_used": False,
        "sealed_labels_used": False,
        "legacy_ai_answers_used": False,
        "identity_used": False,
        "source_packet_unchanged": True,
        "shortlist_uses_outcomes": False,
    }
    serialized = module.canonical_bytes(first).decode("utf-8")
    for forbidden in ("future_return", "mfe", "mae", "legacy_v2_trigger", "case_role", "stock_code"):
        assert forbidden not in serialized.lower()


def test_completed_macd_regime_exposes_full_cycle_boundaries() -> None:
    packet = _load_packet("FP-46f54e49cda2a16d69f9c6d6")
    candidates = module.build_candidates(packet)
    matches = [
        item
        for item in candidates
        if item["basis"] == "MACD_COMPLETED_REGIME_SPAN"
        and item["direction"] == "UP"
        and item["start_date"] == "2022-10-04"
        and item["confirmed_end_date"] == "2023-04-27"
    ]
    assert len(matches) == 1
    assert matches[0]["not_course_anchor_judgement"] is True


def test_current_macd_regime_is_derived_only_to_as_of() -> None:
    packet = _load_packet("FP-18b86f08f05563ff5886097b")
    candidates = module.build_candidates(packet)
    matches = [
        item
        for item in candidates
        if item["basis"] == "MACD_FORMING_REGIME_SPAN"
    ]
    assert len(matches) == 1
    assert matches[0]["start_date"] == "2023-04-14"
    assert matches[0]["confirmed_end_date"] is None
    assert matches[0]["observed_through"] == packet["as_of"]


def test_future_or_reversed_candidate_is_rejected() -> None:
    packet = _load_packet("FP-46f54e49cda2a16d69f9c6d6")
    packet_sha = "a" * 64
    catalog = module.build_catalog(packet, packet_sha, _schema())
    catalog["campaign_candidates"][0]["observed_through"] = "2099-01-01"
    with pytest.raises(ValueError, match="future or reversed"):
        module.validate_catalog(catalog, _schema())


def test_shortlist_is_bounded_and_references_catalog() -> None:
    packet = _load_packet("FP-d79297cd1f01c1600d27cb75")
    catalog = module.build_catalog(packet, "b" * 64, _schema())
    ids = {item["candidate_id"] for item in catalog["campaign_candidates"]}
    assert 0 < len(catalog["prompt_shortlist_campaign_ids"]) <= 80
    assert set(catalog["prompt_shortlist_campaign_ids"]) <= ids
