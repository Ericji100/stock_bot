from __future__ import annotations

import pytest

from scripts import hybrid_v3_atomic_packets_v7 as v7
from scripts import hybrid_v3_atomic_packets_v7_merge_adapter_v1 as adapter


def test_candidate_status_stays_non_locked_and_not_formal() -> None:
    status, locked = adapter.review_manifest_state(v7.BUILDER_STATUS)
    assert status == "BUILT_OUTCOME_BLIND_CANDIDATE_NOT_FORMAL_ACCEPTANCE"
    assert locked is False


def test_existing_final_and_draft_semantics_are_preserved() -> None:
    assert adapter.review_manifest_state("FINAL") == ("LOCKED_OUTCOME_BLIND", True)
    assert adapter.review_manifest_state("DRAFT_NOT_FINAL") == (
        "BUILT_OUTCOME_BLIND_DRAFT",
        False,
    )


def test_unknown_status_still_fails_closed() -> None:
    with pytest.raises(Exception, match="unsupported builder status"):
        adapter.review_manifest_state("UNKNOWN_STATUS")


def test_v7_bytes_match_pin() -> None:
    adapter.assert_v7_frozen()
