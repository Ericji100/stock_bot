"""R8 AS-OF defense candidate scope and offline teacher coverage."""

from __future__ import annotations

import copy

import pytest

from scripts.v2_core_episode_defense_coverage_r8 import build_audit
from scripts.v2_core_episode_defense_shortlist_r8 import INPUT_DIRECTORY, build_for_file, build_shortlist
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, load_json


def test_teacher_stop_is_representable_but_not_always_latest() -> None:
    audit = build_audit(ARTIFACT_DIR)
    assert audit["case_count"] == 14
    assert audit["teacher_stop_visible_count"] == 14
    assert audit["teacher_stop_most_recent_count"] == 13
    assert audit["teacher_stop_signal_close_confirmation_count"] == 1
    assert audit["teacher_answers_used_in_candidate_generation"] is False
    assert audit["selected_defense_or_trade_permission"] is False


def test_signal_close_confirmation_is_visible_and_future_confirmation_rejected() -> None:
    review_id = "FP-f773ac4ff295214dfbea04ac"
    packet_path = ARTIFACT_DIR / INPUT_DIRECTORY / f"{review_id}.json"
    shortlist = build_for_file(packet_path)
    assert any(row["source_date"] == "2023-08-04" and row["availability"] == "CONFIRMED_AT_SIGNAL_CLOSE" for row in shortlist["candidate_rows"])
    assert shortlist["selected_candidate_id"] is None
    packet = copy.deepcopy(load_json(packet_path))
    packet["confirmed_pivots_to_as_of"].append({
        "source_date": "2023-08-04", "confirmation_date": "2023-08-10",
        "price": 49.2063, "scale": "SMALL", "side": "LOW",
    })
    with pytest.raises(ValueError, match="future or backwards-confirmed"):
        build_shortlist(packet)
