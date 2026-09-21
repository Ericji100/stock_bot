"""Cross-role causal constraints stop impossible R7 role combinations."""

from __future__ import annotations

from scripts.v2_core_legacy_role_cross_object_preflight_r7 import build_report, check_cross_objects
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR


def selected(parent_end: str, episode_start: str, *, parent_scale: str = "LARGE", episode_scale: str = "SMALL") -> dict:
    return {
        "ACTIVE_DOWN_CONTROLLER": None,
        "CURRENT_EPISODE_UP": {
            "candidate_id": "episode", "direction": "UP", "status": "FORMING",
            "scale": episode_scale, "start_date": episode_start,
        },
        "UP_CONTROL_CHALLENGER": None,
        "IMMEDIATE_COMPLETED_UP_PARENT": {
            "candidate_id": "parent", "direction": "UP", "status": "CONFIRMED",
            "scale": parent_scale, "confirmed_end_date": parent_end,
        },
        "CONTROLLING_MATURE_CAMPAIGN": None,
    }


def test_parent_must_finish_before_episode_begins() -> None:
    issues = check_cross_objects(selected("2023-05-02", "2023-03-20"))
    assert [issue["code"] for issue in issues] == ["IMMEDIATE_PARENT_NOT_BEFORE_EPISODE"]


def test_big_parent_and_small_episode_allowed_if_temporal_order_valid() -> None:
    assert check_cross_objects(selected("2023-03-01", "2023-03-20")) == []


def test_current_formal_probe_is_blocked_without_teacher_or_future() -> None:
    report = build_report(ARTIFACT_DIR, "FP-18b86f08f05563ff5886097b")
    assert report["status"] == "CROSS_ROLE_UNRESOLVED_NO_TRADE"
    assert [issue["code"] for issue in report["contradictions"]] == ["IMMEDIATE_PARENT_NOT_BEFORE_EPISODE"]
    assert report["teacher_answers_read"] is False
    assert report["trade_permission_granted"] is False
