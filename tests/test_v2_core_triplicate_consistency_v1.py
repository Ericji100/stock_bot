from __future__ import annotations

import json

from scripts.v2_core_triplicate_consistency_v1 import (
    OUT,
    compare,
    core_atom_map,
    markdown,
)


def test_incomplete_triplicate_is_reported_without_fake_rates() -> None:
    report = compare(OUT, OUT / "feasibility_probe_execution_manifest_v6.json")
    assert report["status"] == "NOT_READY_AI_RUNS_INCOMPLETE（AI三輪尚未完成）"
    assert report["expected_case_rounds"] == 144
    assert 0 <= report["completed_case_rounds"] < report["expected_case_rounds"]
    assert (
        report["completed_case_rounds"] + report["missing_case_rounds"]
        == report["expected_case_rounds"]
    )
    assert "rates_percent" not in report
    assert report["future_performance_used"] is False
    assert report["sealed_labels_used"] is False
    assert "不計算一致率" in markdown(report)


def test_complete_v8_validates_all_receipts_and_reports_formal_rates() -> None:
    report = compare(OUT, OUT / "feasibility_probe_execution_manifest_v8.json")
    assert report["status"] == "FEASIBILITY_FAILED（可行性失敗）"
    assert report["case_count"] == 48
    assert report["rates_percent"]["schema_legal_rate"] == 100.0
    assert report["rates_percent"]["causal_legal_rate"] == 100.0
    assert report["rates_percent"]["primary_scenario_consistency_rate"] == 58.33
    assert report["rates_percent"]["final_permission_consistency_rate"] == 39.58
    assert "primary_scenario_consistency_rate" in report["failed_thresholds"]
    assert "final_permission_consistency_rate" in report["failed_thresholds"]
    assert report["future_performance_used"] is False
    assert report["sealed_labels_used"] is False


def test_core_atom_map_uses_results_not_free_text() -> None:
    stage_b = {
        "data_sufficiency": {"result": "PASS"},
        "anchors": [
            {
                "anchor_id": "ANCHOR-A1",
                "atomic": {
                    "ANCHOR_CLEAN": {
                        "result": "UNKNOWN",
                        "reason_code": "free text must not affect comparison",
                    }
                },
            }
        ],
        "shared_structure": {
            "controlling_anchor_id": "ANCHOR-A1",
            "location_remaining_space": {"result": "PASS"},
            "location_not_extended": {"result": "PASS"},
            "exhaustion": {"result": "FAIL"},
            "signal_has_independent_structure": {"result": "PASS"},
        },
        "routing_atoms": {"ACTIVE_LARGE_BEAR_ANCHOR": {"result": "FAIL"}},
    }
    atoms = core_atom_map(stage_b, None)
    assert atoms["stage_b.controlling_anchor.ANCHOR_CLEAN"] == "UNKNOWN"
    assert atoms["stage_b.routing.ACTIVE_LARGE_BEAR_ANCHOR"] == "FAIL"
    assert all("free text" not in value for value in atoms.values())


def test_execution_manifest_contains_no_sealed_labels() -> None:
    path = OUT / "feasibility_probe_execution_manifest_v6.json"
    text = path.read_text(encoding="utf-8").lower()
    assert "expected_permission" not in text
    assert "intended_scenario" not in text
    assert "future_outcome" not in text
    assert json.loads(text)["formal_model"] == "gpt-5.6-sol"
