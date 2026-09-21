"""Legacy-reference integrity flags source facts, not future outcomes."""

from scripts.v2_core_legacy_reference_integrity_audit_v1 import build_report
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR


def test_legacy_reference_integrity_audit() -> None:
    report = build_report(ARTIFACT_DIR)
    assert report["case_count"] == 14
    assert report["source_trigger_matches"] == 14
    assert report["noncanonical_path_label_count"] == 3
    assert report["noncanonical_taiji_label_count"] == 6
    assert report["documented_second_pass_count"] == 2
    assert report["conflicting_stop_evidence_count"] == 2
    assert report["formal_ai_calls"] == 0
    assert report["future_performance_used"] is False
    assert report["locked_reproduction_set_opened"] is False
    conflicts = [row for row in report["rows"] if row["gate_evidence_conflicts_with_selected_stop"]]
    assert all(row["conflicting_gate_stop_equals_signal_day_low"] for row in conflicts)
