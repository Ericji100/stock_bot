from scripts.v2_core_codex_staged_runner_v1 import OUT
from scripts.v2_core_feasibility_failure_analysis_v1 import analyze, atom_family


def test_atom_family_routes_diagnostic_fields() -> None:
    assert atom_family("stage_b.data_sufficiency") == "data_sufficiency"
    assert atom_family("stage_b.routing.ACTIVE_LARGE_BEAR_ANCHOR") == "scenario_routing"
    assert atom_family("stage_b.controlling_anchor.ANCHOR_CLEAN") == "controlling_anchor"
    assert atom_family("stage_d.MATURE_TREND_PULLBACK.gate.X") == "scenario_gate"
    assert atom_family("stage_d.blocking.EXHAUSTION") == "blocking_gate"


def test_v8_failure_analysis_uses_all_completed_cases_without_future_data() -> None:
    report = analyze(OUT, OUT / "feasibility_probe_execution_manifest_v8.json")
    assert report["status"] == "DIAGNOSTIC_COMPLETE（失敗診斷完成）"
    assert report["case_count"] == 48
    assert report["approval_frequency"] == {"0": 36, "1": 10, "2": 2, "3": 0}
    assert report["anchor_signature_unique_counts"] == {"1": 6, "2": 25, "3": 17}
    assert report["trigger_signature_unique_counts"] == {"1": 3, "2": 9, "3": 36}
    assert report["buy_no_buy_consistency_rate"] == 75.0
    assert report["future_performance_used"] is False
    assert report["sealed_labels_used"] is False
