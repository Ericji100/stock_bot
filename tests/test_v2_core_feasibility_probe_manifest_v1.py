from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from scripts.v2_core_feasibility_probe_manifest_v1 import (
    FORBIDDEN_KEYS,
    SCENARIOS,
    contains_forbidden_key,
)


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
PUBLIC = json.loads((OUT / "feasibility_probe_manifest.json").read_text(encoding="utf-8"))
SEALED = json.loads((OUT / "sealed" / "feasibility_probe_calibration_labels.json").read_text(encoding="utf-8"))


def pointer_exists(packet: dict, pointer: str) -> bool:
    value = packet
    for token in pointer.strip("/").split("/"):
        if not token:
            continue
        if isinstance(value, list):
            index = int(token)
            if not 0 <= index < len(value):
                return False
            value = value[index]
        elif isinstance(value, dict) and token in value:
            value = value[token]
        else:
            return False
    return True


def test_probe_has_48_balanced_cases_and_sealed_labels() -> None:
    assert PUBLIC["case_count"] == 48
    assert len(PUBLIC["rows"]) == 48
    assert len(SEALED["rows"]) == 48
    assert len({row["anonymous_stock_id"] for row in PUBLIC["rows"]}) == 48
    counts = Counter((row["intended_scenario"], row["case_role"]) for row in SEALED["rows"])
    for scenario in SCENARIOS:
        assert counts[(scenario, "POSITIVE_REFERENCE")] == 4
        assert counts[(scenario, "NEGATIVE_REFERENCE")] == 4
        assert counts[(scenario, "BOUNDARY_REFERENCE")] == 4


def test_public_manifest_does_not_expose_case_labels_or_identity() -> None:
    assert PUBLIC["selection_labels_sealed"] is True
    assert PUBLIC["future_performance_used_for_selection"] is False
    for row in PUBLIC["rows"]:
        assert "code" not in row
        assert "name" not in row
        assert "case_role" not in row
        assert "intended_scenario" not in row
        assert row["legacy_answer_included"] is False


def test_only_disclosed_bear_supplements_use_partial_reference() -> None:
    partial = [
        row
        for row in SEALED["rows"]
        if row["source_classification"].startswith("PARTIAL_REFERENCE_ONLY")
    ]
    assert len(partial) == 2
    assert all(row["intended_scenario"] == "BEAR_REVERSAL_LEFT_RIGHT" for row in partial)
    assert all(row["case_role"] == "POSITIVE_REFERENCE" for row in partial)


def test_packets_are_asof_blind_and_catalog_refs_resolve() -> None:
    packet_dir = OUT / PUBLIC["packet_directory"]
    for row in PUBLIC["rows"]:
        packet = json.loads((packet_dir / row["packet_file"]).read_text(encoding="utf-8"))
        assert packet["as_of"] == row["as_of"]
        assert packet["daily_structure_context_to_as_of"][-1]["date"] == row["as_of"]
        assert len(packet["daily_structure_context_to_as_of"]) <= 750
        assert contains_forbidden_key(packet) == set()
        assert packet["review_constraints"]["identity_blind"] is True
        assert packet["review_constraints"]["legacy_answer_blind"] is True
        assert packet["review_constraints"]["future_performance_blind"] is True
        refs = [item["ref"] for item in packet["evidence_catalog"]]
        assert len(refs) == len(set(refs))
        assert all(pointer_exists(packet, item["path"]) for item in packet["evidence_catalog"])
        assert {item["ref"] for item in packet["proxy_evidence"]}.issubset(set(refs))


def test_model_and_rounds_are_frozen_for_probe_candidate() -> None:
    assert PUBLIC["formal_model"] == "gpt-5.6-sol"
    assert PUBLIC["reasoning_effort"] == "xhigh"
    assert PUBLIC["required_rounds"] == 3
