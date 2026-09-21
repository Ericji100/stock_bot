from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "hybrid_v3_consistency_v3.py"
SPEC = importlib.util.spec_from_file_location("hybrid_v3_consistency_v3", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def _contract() -> dict:
    return json.loads((ROOT / "config" / "hybrid_multilabel_sampling_v3.json").read_text(encoding="utf-8"))


def _packet(index: int, labels: list[str]) -> dict:
    scenarios = {
        "MATURE_TREND_PULLBACK": [],
        "MACRO_COPY_RESONANCE": [],
        "BEAR_REVERSAL_LEFT_RIGHT": [],
        "FRESH_Q1_EXPANSION": [],
    }
    sufficiency = {
        scenario: {
            "status": False,
            "required_visible_bars": 200 if scenario == "FRESH_Q1_EXPANSION" else 750,
            "actual_visible_bars": 800,
            "reason_code": "INSUFFICIENT_ROUTE_OBJECTIVE_EVIDENCE",
        }
        for scenario in scenarios
    }
    mapping = {
        "V2_CORE_OBJECTIVE_PROXY": "MATURE_TREND_PULLBACK",
        "MACRO_COPY_OBJECTIVE_PROXY": "MACRO_COPY_RESONANCE",
        "FRESH_Q1_OBJECTIVE_PROXY": "FRESH_Q1_EXPANSION",
        "BEAR_REVERSAL_OBJECTIVE_PROXY": "BEAR_REVERSAL_LEFT_RIGHT",
    }
    for label, scenario in mapping.items():
        if label in labels:
            scenarios[scenario] = [{
                "hypothesis_id": f"H-{index}-{scenario}",
                "anchor_ref": f"A-{index}",
                "relation_ref": f"R-{index}",
                "episode_stop_ref": f"E-{index}",
                "campaign_stop_ref": f"C-{index}",
                "position_role": "MOTHER",
            }]
            sufficiency[scenario]["status"] = True
            sufficiency[scenario]["reason_code"] = "SUFFICIENT_ROUTE_HISTORY_AND_OBJECTIVE_EVIDENCE"
    return {
        "review_id": f"D-{index:05d}",
        "anonymous_stock_id": f"S-{index % 73:04d}",
        "as_of": "2023-07-03",
        "question_manifest": {},
        "evidence": [],
        "objective_facts": {
            "scenario_hypotheses": scenarios,
            "data_sufficiency_by_route": sufficiency,
            "parent_campaign_invalidated": "MACRO_DEFENSE_REMOVE_PROXY" in labels,
            "macro_defense_alert": "MACRO_DEFENSE_REMOVE_PROXY" in labels,
            "wait_boundary_reasons": (
                ["MATERIAL_OBJECTIVE_HYPOTHESIS_CONFLICT"]
                if "WAIT_POLICY_BOUNDARY" in labels else []
            ),
            "causal_cutoff_as_of": "2023-07-03",
            "performance_used_for_ordering_or_truncation": False,
        },
    }


def _record(index: int, labels: list[str], primary: str | None = None) -> dict:
    labels = [label for label in module.STRATA if label in labels]
    packet = _packet(index, labels)
    return {
        "source_ordinal": index,
        "review_id": packet["review_id"],
        "anonymous_stock_id": packet["anonymous_stock_id"],
        "eligible_sampling_strata": labels,
        "eligible_sampling_strata_sha256": module.canonical_sha256(labels),
        "primary_sampling_focus": primary or labels[0],
        "packet_sha256": module.canonical_sha256(packet),
        "packet": packet,
    }


def _source() -> list[dict]:
    # Overlapping cases exercise multi-label matching; label-specific slack makes
    # every formal quota and all three blocks feasible.
    rows = [_record(index, list(module.STRATA), primary=module.STRATA[index % len(module.STRATA)]) for index in range(250)]
    # Add label-specific capacity so every 3-block quota has safe slack.
    index = len(rows)
    for label in module.STRATA:
        for _ in range(120):
            rows.append(_record(index, [label]))
            index += 1
    return rows


def test_contract_is_final_and_has_original_six_challenge_quotas() -> None:
    contract = module.load_sampling_contract()
    assert module.SELECTOR_STATUS == "FINAL"
    assert contract["status"] == "FINAL"
    assert tuple(contract["strata_order"]) == module.STRATA
    assert contract["quotas_per_block"] == {
        "V2_CORE_OBJECTIVE_PROXY": 20,
        "MACRO_COPY_OBJECTIVE_PROXY": 15,
        "FRESH_Q1_OBJECTIVE_PROXY": 15,
        "BEAR_REVERSAL_OBJECTIVE_PROXY": 15,
        "MACRO_DEFENSE_REMOVE_PROXY": 20,
        "WAIT_POLICY_BOUNDARY": 35,
    }
    assert sum(contract["quotas_per_block"].values()) == 120
    assert contract["reserve_block_count"] == 2


def test_eligibility_is_exact_multilabel_and_primary_does_not_filter() -> None:
    labels = [
        "MACRO_COPY_OBJECTIVE_PROXY",
        "FRESH_Q1_OBJECTIVE_PROXY",
        "WAIT_POLICY_BOUNDARY",
    ]
    row = _record(1, labels, primary="WAIT_POLICY_BOUNDARY")
    module.validate_source_record(row, _contract())
    assert row["eligible_sampling_strata"] == labels
    assert row["primary_sampling_focus"] == "WAIT_POLICY_BOUNDARY"
    assert len(row["packet"]["objective_facts"]["scenario_hypotheses"]["MACRO_COPY_RESONANCE"]) == 1
    assert len(row["packet"]["objective_facts"]["scenario_hypotheses"]["FRESH_Q1_EXPANSION"]) == 1


@pytest.mark.parametrize(
    "mutator, message",
    [
        (lambda row: row.update(sampling_stratum="WAIT_POLICY_BOUNDARY"), "fields are not exact"),
        (lambda row: row["packet"].update(sampling_focus="WAIT_POLICY_BOUNDARY"), "packet_sha256"),
        (lambda row: row.update(eligible_sampling_strata=list(reversed(row["eligible_sampling_strata"]))), "canonical enum order"),
        (lambda row: row.update(primary_sampling_focus="BEAR_REVERSAL_OBJECTIVE_PROXY"), "must belong"),
        (lambda row: row["eligible_sampling_strata"].pop(), "differs from objective"),
    ],
)
def test_source_contract_fails_closed(mutator, message: str) -> None:
    row = _record(2, ["MACRO_COPY_OBJECTIVE_PROXY", "FRESH_Q1_OBJECTIVE_PROXY"])
    mutator(row)
    with pytest.raises(module.SamplingContractError, match=message):
        module.validate_source_record(row, _contract())


def test_sampling_metadata_never_enters_ai_visible_packet() -> None:
    row = _record(3, ["WAIT_POLICY_BOUNDARY"])
    row["packet"]["sampling_focus"] = "WAIT_POLICY_BOUNDARY"
    row["packet_sha256"] = module.canonical_sha256(row["packet"])
    with pytest.raises(module.SamplingContractError, match="must not contain sampling metadata"):
        module.validate_source_record(row, _contract())

    nested = _record(30, ["WAIT_POLICY_BOUNDARY"])
    nested["packet"]["objective_facts"]["sampling_focus"] = "WAIT_POLICY_BOUNDARY"
    nested["packet_sha256"] = module.canonical_sha256(nested["packet"])
    with pytest.raises(module.SamplingContractError, match="must not contain sampling metadata"):
        module.validate_source_record(nested, _contract())


def test_unknown_wait_reason_and_outcome_field_fail_closed() -> None:
    row = _record(4, ["WAIT_POLICY_BOUNDARY"])
    row["packet"]["objective_facts"]["wait_boundary_reasons"] = ["MADE_UP_REASON"]
    row["packet_sha256"] = module.canonical_sha256(row["packet"])
    with pytest.raises(module.SamplingContractError, match="unknown objective reason"):
        module.validate_source_record(row, _contract())

    row = _record(31, ["WAIT_POLICY_BOUNDARY"])
    row["packet"]["evidence"] = [{"ref": "BAR:FUTURE", "kind": "BAR", "date": "2023-07-04", "values": {}}]
    row["packet_sha256"] = module.canonical_sha256(row["packet"])
    with pytest.raises(module.SamplingContractError, match="evidence date exceeds as_of"):
        module.validate_source_record(row, _contract())

    row = _record(5, ["V2_CORE_OBJECTIVE_PROXY"])
    row["packet"]["objective_facts"]["future_return"] = 99
    row["packet_sha256"] = module.canonical_sha256(row["packet"])
    with pytest.raises(module.SamplingContractError, match="outcome/future"):
        module.validate_source_record(row, _contract())


def test_per_stratum_capacity_requires_primary_plus_two_reserves() -> None:
    contract = _contract()
    rows = _source()
    rows = [row for row in rows if "WAIT_POLICY_BOUNDARY" not in row["eligible_sampling_strata"]]
    with pytest.raises(module.SamplingCapacityError, match=r"quota \* three blocks"):
        module.build_multilabel_holdout_plan(rows, contract=contract)


def test_global_allocation_is_exact_case_disjoint_and_deterministic() -> None:
    contract = _contract()
    rows = _source()
    packets_before = {
        row["review_id"]: hashlib.sha256(module.canonical_json(row["packet"]).encode()).hexdigest()
        for row in rows
    }
    first = module.build_multilabel_holdout_plan(rows, contract=contract)
    second = module.build_multilabel_holdout_plan(deepcopy(rows), contract=deepcopy(contract))
    assert first == second
    assert first["status"] == "LOCKED"
    assert first["selector_status"] == "FINAL"
    assert first["plan_sha256"] == second["plan_sha256"]
    assert first["sampling_focus_is_validation_only"] is True
    assert first["alternative_hypotheses_are_unchanged"] is True
    blocks = [first["primary"], *first["reserve_blocks"]]
    assert [block["case_count"] for block in blocks] == [120, 120, 120]
    assert all(block["sampling_focus_counts"] == contract["quotas_per_block"] for block in blocks)
    selected = [row for block in blocks for row in block["rows"]]
    assert len(selected) == len({row["review_id"] for row in selected}) == 360
    assert all(row["sampling_focus"] in row["eligible_sampling_strata"] for row in selected)
    assert any(len(row["eligible_sampling_strata"]) > 1 for row in selected)
    assert all(
        hashlib.sha256(module.canonical_json(row["packet"]).encode()).hexdigest() == packets_before[row["review_id"]]
        for row in rows
    )


def test_global_max_flow_detects_overlap_capacity_that_individual_counts_miss() -> None:
    contract = _contract()
    # Each stratum individually has >= quota*3, but only 105 distinct cases exist
    # for 360 slots; a naive per-stratum capacity test would double-count them.
    rows = [_record(index, list(module.STRATA)) for index in range(105)]
    assert all(
        sum(stratum in row["eligible_sampling_strata"] for row in rows)
        >= contract["quotas_per_block"][stratum] * 3
        for stratum in module.STRATA
    )
    with pytest.raises(module.SamplingCapacityError, match="global case-disjoint allocation"):
        module.build_multilabel_holdout_plan(rows, contract=contract)


def test_seed_changes_selection_but_not_quota_or_packet_semantics() -> None:
    rows = _source()
    first_contract = _contract()
    second_contract = deepcopy(first_contract)
    second_contract["seed"] = "ANOTHER_FROZEN_SEED"
    first = module.build_multilabel_holdout_plan(rows, contract=first_contract)
    second = module.build_multilabel_holdout_plan(rows, contract=second_contract)
    first_ids = {row["review_id"] for row in first["primary"]["rows"]}
    second_ids = {row["review_id"] for row in second["primary"]["rows"]}
    assert first_ids != second_ids
    assert first["primary"]["sampling_focus_counts"] == second["primary"]["sampling_focus_counts"]


def test_primary_sampling_focus_does_not_constrain_global_assignment() -> None:
    rows = _source()
    changed = deepcopy(rows)
    for row in changed:
        row["primary_sampling_focus"] = row["eligible_sampling_strata"][-1]
    first = module.build_multilabel_holdout_plan(rows, contract=_contract())
    second = module.build_multilabel_holdout_plan(changed, contract=_contract())
    allocation = lambda plan: [
        (block["block_id"], row["review_id"], row["sampling_focus"])
        for block in [plan["primary"], *plan["reserve_blocks"]]
        for row in block["rows"]
    ]
    assert allocation(first) == allocation(second)


def test_plan_validation_recomputes_and_rejects_tamper() -> None:
    rows = _source()
    contract = _contract()
    plan = module.build_multilabel_holdout_plan(rows, contract=contract)
    result = module.validate_multilabel_holdout_plan(plan, rows, contract=contract)
    assert result["valid"] is True
    assert result["allocated_cases"] == 360
    tampered = deepcopy(plan)
    tampered["primary"]["rows"][0]["sampling_focus"] = "WAIT_POLICY_BOUNDARY"
    with pytest.raises(module.SamplingContractError, match="deterministic recomputation"):
        module.validate_multilabel_holdout_plan(tampered, rows, contract=contract)
