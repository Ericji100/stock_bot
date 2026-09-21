"""AS-OF chronology permits cross-scale anchor/defense overlap without deciding it."""

import copy

from scripts.v2_core_legacy_hierarchy_temporal_r8 import check_temporal_facts
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, load_json


def pair(review_id: str, anchor_id: str, defense_date: str) -> tuple[dict, dict, str]:
    packet = load_json(ARTIFACT_DIR / "legacy_anchor_alignment_input_packets_candidate_r1" / f"{review_id}.json")
    shortlist = load_json(ARTIFACT_DIR / "episode_defense_shortlists_candidate_r8" / f"{review_id}.json")
    anchor = next(row for row in packet["candidate_pool"] if row["candidate_id"] == anchor_id)
    defense = next(row for row in shortlist["candidate_rows"] if row["source_date"] == defense_date)
    return anchor, defense, packet["as_of"]


def test_completed_macro_anchor_can_overlap_small_defense() -> None:
    anchor, defense, as_of = pair("FP-38178c2820dd71aa800fd8d7", "CAMSEG-54af82308bdf7073fa16", "2023-04-21")
    result = check_temporal_facts(anchor, defense, as_of)
    assert result["status"] == "LEGAL_AS_OF"
    assert result["temporal_relation"] == "ANCHOR_SPANS_DEFENSE_START"
    assert result["cross_scale_overlap_possible"] is True
    assert result["causal_control_decided"] is False
    assert result["trade_permission_granted"] is False


def test_forming_fresh_anchor_and_signal_close_defense_are_asof_legal() -> None:
    anchor, defense, as_of = pair("FP-18b86f08f05563ff5886097b", "CAMSEG-f150eeeb912b887d07de", "2023-04-24")
    assert check_temporal_facts(anchor, defense, as_of)["status"] == "LEGAL_AS_OF"
    # The second packet's defense can be confirmed exactly at signal close.
    packet2 = load_json(ARTIFACT_DIR / "legacy_anchor_alignment_input_packets_candidate_r1" / "FP-f773ac4ff295214dfbea04ac.json")
    shortlist2 = load_json(ARTIFACT_DIR / "episode_defense_shortlists_candidate_r8" / "FP-f773ac4ff295214dfbea04ac.json")
    defense2 = next(row for row in shortlist2["candidate_rows"] if row["source_date"] == "2023-08-04")
    anchor2 = next(row for row in packet2["candidate_pool"] if row["status"] == "FORMING" and row["direction"] == "UP" and row["start_date"] <= defense2["source_date"])
    assert check_temporal_facts(anchor2, defense2, packet2["as_of"])["status"] == "LEGAL_AS_OF"


def test_future_confirmation_is_rejected() -> None:
    anchor, defense, as_of = pair("FP-38178c2820dd71aa800fd8d7", "CAMSEG-54af82308bdf7073fa16", "2023-04-21")
    changed = copy.deepcopy(defense)
    changed["confirmation_date"] = "2023-05-05"
    result = check_temporal_facts(anchor, changed, as_of)
    assert result["status"] == "INVALID_AS_OF"
    assert result["trade_permission_granted"] is False
