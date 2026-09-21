"""R7B five-object probe shares one frozen episode and no causal reversal."""

from __future__ import annotations

from scripts.v2_core_legacy_role_cross_object_preflight_r7b import build_report
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR


def test_formal_probe_has_coherent_bound_objects_but_no_trade_or_teacher_claim() -> None:
    report = build_report(ARTIFACT_DIR, "FP-18b86f08f05563ff5886097b")
    assert report["status"] == "CROSS_ROLE_PRELIMINARY_READY"
    assert report["contradictions"] == []
    roles = report["role_results"]
    assert roles["CURRENT_EPISODE_UP"]["role_status"] == "SELECTED"
    assert roles["IMMEDIATE_COMPLETED_UP_PARENT"]["role_status"] == "NOT_APPLICABLE"
    assert roles["UP_CONTROL_CHALLENGER"]["role_status"] == "SELECTED"
    assert report["teacher_answers_read"] is False
    assert report["trade_permission_granted"] is False
