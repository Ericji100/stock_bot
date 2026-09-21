from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from scripts.v2_core_dataset_split_v1 import (
    SCENARIOS,
    assign_groups,
    build_payloads,
    load_formal_rows,
    row_scenarios,
)


ROOT = Path(__file__).resolve().parents[1]
KEY = bytes.fromhex("11" * 32)


def test_formal_rows_only_include_complete_audits() -> None:
    included, excluded = load_formal_rows(ROOT)
    assert len(included) == 1778
    assert len(excluded) == 140
    assert {row["batch_id"] for row in excluded} == {"889"}


def test_stock_groups_never_cross_partitions() -> None:
    included, _ = load_formal_rows(ROOT)
    groups = defaultdict(list)
    for row in included:
        groups[str(row["decision"]["code"])].append(row)
    assignment = assign_groups(dict(groups))

    assert set(assignment) == set(groups)
    assert set(assignment.values()) == {
        "CALIBRATION_SET",
        "LOCKED_REPRODUCTION_SET",
    }
    for code, rows in groups.items():
        assert len({assignment[str(row["decision"]["code"])] for row in rows}) == 1
        assert assignment[code] in {"CALIBRATION_SET", "LOCKED_REPRODUCTION_SET"}


def test_each_observed_scenario_has_groups_on_both_sides() -> None:
    included, _ = load_formal_rows(ROOT)
    groups = defaultdict(list)
    for row in included:
        groups[str(row["decision"]["code"])].append(row)
    assignment = assign_groups(dict(groups))

    for scenario in SCENARIOS:
        scenario_codes = {
            code
            for code, rows in groups.items()
            if any(scenario in row_scenarios(row["decision"]) for row in rows)
        }
        assert len(scenario_codes) >= 2
        assert {assignment[code] for code in scenario_codes} == {
            "CALIBRATION_SET",
            "LOCKED_REPRODUCTION_SET",
        }


def test_locked_public_manifest_contains_no_identity_or_legacy_answers() -> None:
    payloads = build_payloads(ROOT, KEY)
    locked = payloads["locked"]

    assert locked["identity_visible"] is False
    assert locked["legacy_answers_visible"] is False
    for row in locked["rows"]:
        assert "code" not in row
        assert "name" not in row
        assert "legacy_v1" not in row
        assert "legacy_v2" not in row
        assert "signal_date" not in row
        assert "scenario" not in row


def test_build_is_deterministic_for_same_key() -> None:
    first = build_payloads(ROOT, KEY)
    second = build_payloads(ROOT, KEY)
    assert first["calibration"] == second["calibration"]
    assert first["locked"] == second["locked"]
    assert first["sealed"] == second["sealed"]
