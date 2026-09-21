"""Representation-only R7-v2 regression tests."""

from __future__ import annotations

from scripts.v2_core_legacy_role_shortlist_r7 import ROLE_LIMITS
from scripts.v2_core_legacy_role_shortlist_r7_v2 import build_shortlists
from tests.test_v2_core_legacy_role_shortlist_r7 import candidate, packet


def test_formation_bearing_roles_keep_old_forming_up_without_teacher_lookup() -> None:
    rows = [
        candidate(i, "UP", "CONFIRMED", "PIVOT_CONFIRMED_CAMPAIGN" if i % 2 else "MACD_COMPLETED_REGIME_SPAN")
        for i in range(80)
    ]
    formation = candidate(80, "UP", "FORMING", "PIVOT_FORMING_CAMPAIGN")
    formation["start_date"] = "2023-01-01"
    formation["observed_through"] = "2023-02-01"
    rows.append(formation)
    result = build_shortlists(packet(rows))
    for role in ("UP_CONTROL_CHALLENGER", "CONTROLLING_MATURE_CAMPAIGN"):
        payload = result["roles"][role]
        ids = {row["candidate_id"] for row in payload["candidate_rows"]}
        assert formation["candidate_id"] in ids
        assert payload["shortlist_count"] == ROLE_LIMITS[role]
        assert next(row for row in payload["candidate_rows"] if row["candidate_id"] == formation["candidate_id"])["selection_reasons"] == ["ALL_FORMING_IN_ROLE_UNIVERSE"]


def test_completed_parent_remains_confirmed_only() -> None:
    rows = [candidate(0, "UP", "FORMING", "MACD_FORMING_REGIME_SPAN"), candidate(1, "UP", "CONFIRMED")]
    result = build_shortlists(packet(rows))
    assert {row["candidate_id"] for row in result["roles"]["IMMEDIATE_COMPLETED_UP_PARENT"]["candidate_rows"]} == {"CAMSEG-0001"}
    assert result["contract"]["teacher_answers_read"] is False
    assert result["contract"]["course_role_selected_by_program"] is False
