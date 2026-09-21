import json
from pathlib import Path

from scripts import hybrid_v3_mature_s1_track_v1 as track


def _record(index: int, focus: str) -> dict:
    return {
        "source_ordinal": index,
        "review_id": f"R-{index}",
        "anonymous_stock_id": f"S-{index}",
        "packet_sha256": f"{index + 1:064x}",
        "as_of": f"2023-{6 + index % 6:02d}-01",
        "eligible_sampling_strata": [],
        "eligible_stage_focuses": [focus],
    }


def test_locked_stage_protocol_has_exact_case_coverage() -> None:
    value = json.loads(track.STAGE_PROTOCOL.read_text(encoding="utf-8"))
    sample = value["consistency_sample"]
    assert value["status"] == "FINAL_RESEARCH_LOCKED"
    assert value["target"]["primary_scenario"] == "MATURE_TREND_PULLBACK"
    assert value["target"]["trade_route"] == "V2_CORE"
    assert list(sample["quotas"]) == sample["focus_order"]
    assert sum(sample["quotas"].values()) == sample["cases"] == 36


def test_stage_focuses_keep_mature_and_negative_controls_separate() -> None:
    base = {
        "eligible_sampling_strata": [
            "V2_CORE_OBJECTIVE_PROXY",
            "MACRO_DEFENSE_REMOVE_PROXY",
            "WAIT_POLICY_BOUNDARY",
        ],
        "packet": {
            "objective_facts": {
                "wait_boundary_reasons": [
                    "UP_ATTACK_WITHOUT_CAUSAL_STOP",
                    "MATERIAL_OBJECTIVE_HYPOTHESIS_CONFLICT",
                ]
            }
        },
    }
    assert track.stage_focuses(base) == [
        "MATURE_OBJECTIVE_PROXY",
        "MACRO_DEFENSE_REMOVE",
        "WAIT_NO_CAUSAL_STOP",
        "WAIT_MATERIAL_CONFLICT",
    ]
    macro_only = {
        "eligible_sampling_strata": ["MACRO_COPY_OBJECTIVE_PROXY"],
        "packet": {"objective_facts": {"wait_boundary_reasons": []}},
    }
    assert track.stage_focuses(macro_only) == ["COMPETING_MACRO_ONLY"]


def test_allocator_fills_focuses_with_distinct_stocks_and_months() -> None:
    focuses = [
        "MATURE_OBJECTIVE_PROXY",
        "COMPETING_MACRO_ONLY",
        "COMPETING_FRESH_ONLY",
        "COMPETING_BEAR_ONLY",
        "MACRO_DEFENSE_REMOVE",
        "WAIT_NO_CAUSAL_STOP",
        "WAIT_MATERIAL_CONFLICT",
    ]
    records = [_record(index, focus) for index, focus in enumerate(focuses)]
    protocol = {
        "consistency_sample": {
            "seed": "TEST",
            "focus_order": focuses,
            "quotas": {focus: 1 for focus in focuses},
            "minimum_distinct_months": 6,
            "maximum_cases_per_month": 2,
        }
    }
    selected = track.allocate(records, protocol)
    assert len(selected) == len(focuses)
    assert len({row["anonymous_stock_id"] for row in selected}) == len(focuses)
    assert [row["sampling_focus"] for row in selected] == focuses


def test_existing_frozen_runtime_files_are_not_stage_files() -> None:
    stage_paths = {
        track.STAGE_PROTOCOL.resolve(),
        Path(track.__file__).resolve(),
        track.CONSISTENCY_EVALUATOR.resolve(),
    }
    runtime_paths = {
        track.PROMPT.resolve(),
        track.SCHEMA.resolve(),
        track.RESEARCH_PROTOCOL.resolve(),
        track.LAUNCHER.resolve(),
        track.REVIEWER.resolve(),
        track.RUNNER.resolve(),
        track.POLICY.resolve(),
    }
    assert stage_paths.isdisjoint(runtime_paths)
