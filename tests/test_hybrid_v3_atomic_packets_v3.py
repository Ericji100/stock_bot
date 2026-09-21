from __future__ import annotations

import math
import json
from pathlib import Path

import pandas as pd
import pytest

from scripts import hybrid_v3_atomic_packets_v3 as packets_v3

from scripts.hybrid_v3_atomic_packets_v3 import (
    BUILDER_STATUS,
    BUILDER_VERSION,
    DEFAULT_ATOMIC_SCHEMA,
    FORMAL_MONITORED_STOCK_DAYS,
    FORMAL_RESERVE_BLOCK_COUNT,
    FORMAL_SAMPLING_QUOTAS,
    FORMAL_SOURCE_FILE,
    FORMAL_SOURCE_MANIFEST_FILE,
    FORMAL_STOCK_COUNT,
    PACKET_VERSION,
    SAMPLING_STRATA_CANONICAL_ORDER,
    SAMPLING_CONTRACT_VERSION,
    WAIT_BOUNDARY_REASON_ENUM,
    FormalBuildIntegrityError,
    _relation_candidates,
    _scenario_hypotheses,
    build_atomic_packet,
    build_daily_objective_states,
    build_formal_shard,
    eligible_sampling_strata,
    file_sha256,
    load_and_validate_sampling_contract,
    merge_formal_build_shards,
    prepare_formal_build_plan,
    primary_sampling_focus,
    sampling_capacity_report,
)


def _anchor(
    name: str,
    *,
    direction: str,
    scale: str = "LARGE",
    available: str = "2023-01-09",
    number: int = 1,
) -> dict:
    return {
        "candidate_id": f"ANCHOR_CANDIDATE:{name}",
        "objective_hypothesis_id": f"H-{name}",
        "hypothesis_type": "PIVOT_LEG",
        "scale": scale,
        "direction_hint": direction,
        "start_ref": f"BAR:2023-01-0{max(1, number)}",
        "end_ref": f"BAR:{available}",
        "available_on": available,
        "amplitude": 2.0,
        "macd_support_only": False,
        "campaign_id": "C-1",
        "taiji_generation": "ANCHOR_LEG_1" if number == 1 else "COPY_LEG_3",
        "same_direction_attack_number": number,
        "completed_prior_copy_count": max(0, number - 2),
        "pivot_definition": "LEGACY_RADIUS_3_10_NOT_COURSE_L1_L2",
        "asserted_valid_anchor": False,
    }


def _objective(
    as_of: str,
    *,
    large_dow: str = "BULL",
    large_control: str = "UP_CONTROL",
    phase: str = "NONE",
    boundary_status: str | None = None,
) -> dict:
    boundary_ref = "DEFENSE:LARGE:BEAR"
    defenses = []
    if boundary_status is not None:
        defenses.append(
            {
                "defense_id": boundary_ref,
                "scale": "LARGE",
                "side": "BEARISH",
                "status": boundary_status,
                "established_on": "2023-01-08",
                "price": 12.0,
                "source_pivot_ref": "PIVOT:LARGE:HIGH:A",
                "control_pivot_ref": "PIVOT:LARGE:LOW:B",
                "campaign_id": "C-DOWN",
            }
        )
    return {
        "as_of": as_of,
        "attacks": [
            {
                "attack_id": "ATTACK:SMALL:UP:TODAY",
                "scale": "SMALL",
                "direction": "UP",
                "confirmed_on": as_of,
            }
        ],
        "defenses": defenses,
        "dow": {"large": {"dow_state": large_dow}},
        "controls": {"large": {"state": large_control}},
        "left_right": {"phase": phase, "boundary_defense_ref": boundary_ref},
    }


def _stop_candidates() -> list[dict]:
    return [
        {"candidate_id": "STOP_CANDIDATE:SMALL", "side": "BULLISH", "scale": "SMALL"},
        {"candidate_id": "STOP_CANDIDATE:LARGE", "side": "BULLISH", "scale": "LARGE"},
    ]


def _route_packet(labels: list[str], wait_reasons: list[str] | None = None) -> dict:
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
    route_map = {
        "V2_CORE_OBJECTIVE_PROXY": "MATURE_TREND_PULLBACK",
        "MACRO_COPY_OBJECTIVE_PROXY": "MACRO_COPY_RESONANCE",
        "FRESH_Q1_OBJECTIVE_PROXY": "FRESH_Q1_EXPANSION",
        "BEAR_REVERSAL_OBJECTIVE_PROXY": "BEAR_REVERSAL_LEFT_RIGHT",
    }
    for label, scenario in route_map.items():
        if label in labels:
            scenarios[scenario] = [{"hypothesis_id": f"H-{scenario}"}]
            sufficiency[scenario]["status"] = True
            sufficiency[scenario]["reason_code"] = (
                "SUFFICIENT_ROUTE_HISTORY_AND_OBJECTIVE_EVIDENCE"
            )
    return {
        "objective_facts": {
            "scenario_hypotheses": scenarios,
            "data_sufficiency_by_route": sufficiency,
            "parent_campaign_invalidated": "MACRO_DEFENSE_REMOVE_PROXY" in labels,
            "macro_defense_alert": "MACRO_DEFENSE_REMOVE_PROXY" in labels,
            "wait_boundary_reasons": wait_reasons or [],
        }
    }


def _frame(count: int = 240) -> pd.DataFrame:
    dates = pd.bdate_range("2022-01-03", periods=count)
    close = [50 + index * 0.04 + 3 * math.sin(index / 8) for index in range(count)]
    return pd.DataFrame(
        {
            "date": dates,
            "open": [value - 0.2 for value in close],
            "high": [value + 0.7 for value in close],
            "low": [value - 0.8 for value in close],
            "close": close,
            "volume": [1000 + (index % 17) * 20 for index in range(count)],
        }
    )


def _day(frame: pd.DataFrame, index: int) -> str:
    return pd.Timestamp(frame.iloc[index]["date"]).date().isoformat()


def test_candidate2_component_metadata_and_formal_scope_are_isolated() -> None:
    assert PACKET_VERSION == "hybrid-v3-atomic-question-packet-v3"
    assert BUILDER_VERSION == "hybrid-v3-atomic-packets-v3"
    assert BUILDER_STATUS == "FINAL"
    assert FORMAL_STOCK_COUNT == 1029
    assert FORMAL_MONITORED_STOCK_DAYS == 151804


def test_versioned_sampling_contract_is_strictly_cross_checked(tmp_path: Path) -> None:
    contract = load_and_validate_sampling_contract()
    assert contract["contract_version"] == SAMPLING_CONTRACT_VERSION
    assert tuple(contract["strata_order"]) == SAMPLING_STRATA_CANONICAL_ORDER
    drifted = dict(contract)
    drifted["quotas_per_block"] = dict(contract["quotas_per_block"])
    drifted["quotas_per_block"]["WAIT_POLICY_BOUNDARY"] += 1
    path = tmp_path / "drifted-contract.json"
    path.write_text(json.dumps(drifted), encoding="utf-8")
    with pytest.raises(FormalBuildIntegrityError, match="quotas differ"):
        load_and_validate_sampling_contract(path)


def test_macro_chain_requires_up_down_and_current_up_while_fresh_has_no_parent_copy() -> None:
    as_of = "2023-01-10"
    parent = _anchor("PARENT", direction="UP", available="2023-01-07", number=1)
    correction = _anchor("CORRECTION", direction="DOWN", available="2023-01-09", number=1)
    objective = _objective(as_of)
    macro_relations = _relation_candidates([parent, correction], objective, as_of)
    macro = next(row for row in macro_relations if "MACRO_COPY_RESONANCE" in row["eligible_scenarios"])
    assert (macro["parent_direction"], macro["correction_direction"], macro["current_direction"]) == (
        "UP", "DOWN", "UP"
    )
    assert macro["current_available_on"] == as_of
    assert "FRESH_Q1_EXPANSION" not in macro["eligible_scenarios"]

    fresh_anchor = _anchor("FRESH", direction="UP", available="2023-01-09", number=1)
    fresh_relations = _relation_candidates([fresh_anchor], objective, as_of)
    fresh = next(row for row in fresh_relations if "FRESH_Q1_EXPANSION" in row["eligible_scenarios"])
    assert fresh["parent_anchor_ref"] is None
    assert fresh["current_direction"] == "UP"
    assert fresh["candidate_id"] != macro["candidate_id"]


def test_bear_route_requires_large_bear_control_valid_boundary_and_current_small_up() -> None:
    as_of = "2023-01-10"
    down_parent = _anchor("DOWN", direction="DOWN", available="2023-01-08")
    valid = _relation_candidates(
        [down_parent],
        _objective(
            as_of,
            large_dow="BEAR",
            large_control="DOWN_CONTROL",
            phase="LR",
            boundary_status="ACTIVE",
        ),
        as_of,
    )
    bear = next(row for row in valid if "BEAR_REVERSAL_LEFT_RIGHT" in row["eligible_scenarios"])
    assert bear["parent_direction"] == "DOWN"
    assert bear["parent_scale"] == "LARGE"
    assert bear["current_direction"] == "UP"
    assert bear["current_scale"] == "SMALL"
    assert bear["left_right_phase"] == "LR"
    assert bear["large_bear_defense_status"] == "ACTIVE"

    invalid = _relation_candidates(
        [down_parent],
        _objective(as_of, large_dow="BULL", large_control="UP_CONTROL", phase="LR", boundary_status="ACTIVE"),
        as_of,
    )
    assert all("BEAR_REVERSAL_LEFT_RIGHT" not in row["eligible_scenarios"] for row in invalid)


def test_scenario_hypotheses_preserve_typed_relation_and_current_up() -> None:
    as_of = "2023-01-10"
    anchors = [
        _anchor("PARENT", direction="UP", available="2023-01-07", number=1),
        _anchor("CORRECTION", direction="DOWN", available="2023-01-09", number=1),
    ]
    objective = _objective(as_of)
    relations = _relation_candidates(anchors, objective, as_of)
    candidates = {
        "anchor_candidates": anchors,
        "relation_candidates": relations,
        "stop_candidates": _stop_candidates(),
    }
    hypotheses = _scenario_hypotheses(candidates, position_role="MOTHER", objective=objective)
    assert hypotheses["MACRO_COPY_RESONANCE"]
    assert not hypotheses["FRESH_Q1_EXPANSION"]
    relation_by_id = {row["candidate_id"]: row for row in relations}
    assert all(
        relation_by_id[row["relation_ref"]]["current_direction"] == "UP"
        for rows in hypotheses.values()
        for row in rows
    )


def test_multilabel_eligibility_retains_overlap_and_primary_never_truncates() -> None:
    labels = list(SAMPLING_STRATA_CANONICAL_ORDER)
    packet = _route_packet(labels, ["MATERIAL_OBJECTIVE_HYPOTHESIS_CONFLICT"])
    eligible = eligible_sampling_strata(packet)
    assert eligible == labels
    assert primary_sampling_focus(eligible) in eligible
    assert len(eligible) == 6


def test_capacity_requires_each_original_quota_times_three_and_wait_105() -> None:
    assert FORMAL_RESERVE_BLOCK_COUNT == 2
    rows = [
        {"eligible_sampling_strata": list(SAMPLING_STRATA_CANONICAL_ORDER)}
        for _ in range(105)
    ]
    report = sampling_capacity_report(rows)
    assert report["layers"]["WAIT_POLICY_BOUNDARY"]["required_cases"] == 105
    assert all(
        report["layers"][stratum]["required_cases"] == FORMAL_SAMPLING_QUOTAS[stratum] * 3
        for stratum in SAMPLING_STRATA_CANONICAL_ORDER
    )
    assert report["per_layer_capacity_pass"] is True
    assert report["global_case_disjoint_allocation_verified"] is False
    short = sampling_capacity_report(rows[:-1])
    assert short["layers"]["WAIT_POLICY_BOUNDARY"]["capacity_pass"] is False
    assert short["per_layer_capacity_pass"] is False


def test_initial_selection_is_a_bounded_wait_review_but_refresh_is_not_daily_expansion() -> None:
    frame = _frame()
    monitor = _day(frame, 200)
    refresh = _day(frame, 205)
    as_of = _day(frame, 210)
    review = {
        "selection_timeline": {monitor: ["A"], refresh: ["B"]},
        "confirmed_pivots": [],
        "macd_21_55_55_cycles": [],
    }
    visible, states = build_daily_objective_states(
        frame, review, monitor_on=monitor, as_of=as_of
    )
    by_day = {row["as_of"]: row for row in states}
    assert by_day[monitor]["review_required"] is True
    assert by_day[monitor]["lazy_review_reasons"] == ["INITIAL_SELECTION_BOUNDARY"]
    assert by_day[refresh]["review_required"] is False
    built = build_atomic_packet(
        anonymous_stock_id="S-0123456789abcdef",
        review_id="D-0123456789abcdef01234567",
        as_of=monitor,
        visible_frame=visible,
        daily_state=by_day[monitor],
        review_packet=review,
    )
    assert built["objective_facts"]["wait_boundary_reasons"] == [
        "INITIAL_SELECTION_BOUNDARY"
    ]
    assert eligible_sampling_strata(built) == ["WAIT_POLICY_BOUNDARY"]
    assert tuple(WAIT_BOUNDARY_REASON_ENUM) == (
        "INITIAL_SELECTION_BOUNDARY",
        "RESELECTION_BOUNDARY",
        "UP_ATTACK_WITHOUT_CAUSAL_STOP",
        "UP_ATTACK_WITH_INSUFFICIENT_ROUTE_OBJECTIVE_EVIDENCE",
        "MATERIAL_OBJECTIVE_HYPOTHESIS_CONFLICT",
    )


def test_remove_freezes_candidate2_until_explicit_reselection_boundary() -> None:
    dates = pd.bdate_range("2020-01-02", periods=800)
    close = [10.0] * len(dates)
    close[779:791] = [10, 10, 12, 7, 8, 8, 8, 10, 10, 10, 10, 12]
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": [value + 0.4 for value in close],
            "low": [value - 0.4 for value in close],
            "close": close,
            "volume": [1000] * len(close),
        }
    )
    pivot_rows = [
        ("LARGE", "HIGH", 760, 761, 11),
        ("LARGE", "LOW", 776, 777, 8),
        ("SMALL", "HIGH", 760, 761, 10.5),
        ("SMALL", "LOW", 776, 777, 8.5),
        ("SMALL", "HIGH", 783, 784, 9),
        ("SMALL", "LOW", 784, 785, 7.5),
        ("SMALL", "HIGH", 787, 788, 11),
        ("SMALL", "LOW", 788, 789, 8),
    ]
    review = {
        "selection_timeline": {_day(frame, 779): ["A"], _day(frame, 788): ["B"]},
        "confirmed_pivots": [
            {
                "scale": scale,
                "side": side,
                "source_date": _day(frame, source),
                "confirmation_date": _day(frame, confirmation),
                "price": price,
            }
            for scale, side, source, confirmation, price in pivot_rows
        ],
        "macd_21_55_55_cycles": [],
    }
    _, states = build_daily_objective_states(
        frame,
        review,
        monitor_on=_day(frame, 779),
        as_of=_day(frame, 792),
    )
    by_day = {row["as_of"]: row for row in states}
    removed = by_day[_day(frame, 782)]
    assert removed["remove_frontier"] is True
    assert removed["review_required"] is True
    inactive_signal = by_day[_day(frame, 786)]
    assert inactive_signal["active_watchlist"] is False
    assert inactive_signal["review_required"] is False
    assert inactive_signal["skip_reason"] == "WATCHLIST_INACTIVE_AWAIT_RESELECTION"
    reselected = by_day[_day(frame, 788)]
    assert reselected["active_watchlist"] is True
    assert reselected["review_required"] is True
    assert "RESELECTION_BOUNDARY" in reselected["lazy_review_reasons"]
    assert reselected["campaign_generation"] == 2


def _formal_source_fixture(tmp_path: Path, stock_count: int = 3) -> dict:
    private = tmp_path / "private"
    prices = private / "prices"
    reviews = private / "reviews"
    prices.mkdir(parents=True)
    reviews.mkdir(parents=True)
    frame = _frame()
    monitor_on = _day(frame, 200)
    as_of = _day(frame, 210)
    review = {
        "selection_timeline": {monitor_on: ["A"]},
        "confirmed_pivots": [],
        "macd_21_55_55_cycles": [],
    }
    monitored_sessions = int(
        (
            (pd.to_datetime(frame["date"]) >= pd.Timestamp(monitor_on))
            & (pd.to_datetime(frame["date"]) <= pd.Timestamp(as_of))
        ).sum()
    )
    input_items = []
    packet_items = []
    identities = []
    for index in range(stock_count):
        code = f"T{index:03d}"
        anonymous_id = f"S-{index:016x}"
        price_path = prices / f"source-{index:03d}.csv"
        review_path = reviews / f"source-{index:03d}.json"
        frame.to_csv(price_path, index=False)
        review_path.write_text(json.dumps(review), encoding="utf-8")
        input_items.append(
            {
                "code": code,
                "monitor_on": monitor_on,
                "price_path": str(price_path),
                "price_sha256": file_sha256(price_path),
            }
        )
        packet_items.append(
            {
                "code": code,
                "monitor_on": monitor_on,
                "packet": str(review_path),
                "packet_sha256": file_sha256(review_path),
                "monitored_sessions": monitored_sessions,
            }
        )
        identities.append({"index": index, "code": code, "anonymous_id": anonymous_id})
    input_manifest = private / "input_manifest.json"
    packet_manifest = private / "packet_manifest.json"
    identity_map = private / "sealed_identity_map.json"
    freeze_source_manifest = private / "freeze_source_manifest.json"
    input_manifest.write_text(
        json.dumps(
            {"prepared_stock_count": stock_count, "as_of": as_of, "items": input_items}
        ),
        encoding="utf-8",
    )
    packet_manifest.write_text(
        json.dumps({"as_of": as_of, "items": packet_items}), encoding="utf-8"
    )
    identity_map.write_text(
        json.dumps({"salt_hex": "12" * 32, "mapping": identities}), encoding="utf-8"
    )
    freeze_source_manifest.write_text(
        json.dumps({"stocks": stock_count, "stock_days": monitored_sessions * stock_count}),
        encoding="utf-8",
    )
    return {
        "input_manifest_path": input_manifest,
        "packet_manifest_path": packet_manifest,
        "identity_map_path": identity_map,
        "atomic_schema_path": DEFAULT_ATOMIC_SCHEMA,
        "freeze_source_manifest_path": freeze_source_manifest,
        "work_dir": tmp_path / "public-work",
        "formal_output_dir": tmp_path / "ai-source",
        "expected_stock_count": stock_count,
        "expected_stock_days": monitored_sessions * stock_count,
    }


def test_formal_artifacts_pin_contract_and_drift_blocks_resume_and_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract_path = tmp_path / "sampling-contract.json"
    contract_path.write_bytes(packets_v3.DEFAULT_SAMPLING_CONTRACT.read_bytes())
    monkeypatch.setattr(packets_v3, "DEFAULT_SAMPLING_CONTRACT", contract_path)
    kwargs = _formal_source_fixture(tmp_path)
    expected_pin = {
        "sampling_contract_version": SAMPLING_CONTRACT_VERSION,
        "sampling_contract_status": "FINAL",
        "sampling_contract_sha256": file_sha256(contract_path),
    }
    plan = prepare_formal_build_plan(**kwargs)
    assert all(plan[key] == value for key, value in expected_pin.items())
    for shard_id in range(3):
        shard = build_formal_shard(**kwargs, shard_id=shard_id)
        assert all(shard[key] == value for key, value in expected_pin.items())
    fragment_manifests = sorted(kwargs["work_dir"].glob("shard_*/stocks/*.manifest.json"))
    assert len(fragment_manifests) == 3
    for path in fragment_manifests:
        fragment = json.loads(path.read_text(encoding="utf-8"))
        assert all(fragment[key] == value for key, value in expected_pin.items())
    manifest = merge_formal_build_shards(**kwargs)
    assert all(manifest[key] == value for key, value in expected_pin.items())
    assert manifest["builder_status"] == "FINAL"
    assert manifest["status"] == "LOCKED_OUTCOME_BLIND"
    assert manifest["source_order_locked"] is True
    records = [
        json.loads(line)
        for line in (kwargs["formal_output_dir"] / FORMAL_SOURCE_FILE)
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert records
    assert all(
        set(row)
        == {
            "source_ordinal",
            "review_id",
            "anonymous_stock_id",
            "eligible_sampling_strata",
            "eligible_sampling_strata_sha256",
            "primary_sampling_focus",
            "packet_sha256",
            "packet",
        }
        for row in records
    )
    persisted_manifest = json.loads(
        (kwargs["formal_output_dir"] / FORMAL_SOURCE_MANIFEST_FILE).read_text(encoding="utf-8")
    )
    assert all(persisted_manifest[key] == value for key, value in expected_pin.items())

    drifted = json.loads(contract_path.read_text(encoding="utf-8"))
    drifted["seed"] = "DRIFTED_AFTER_FORMAL_PLAN"
    contract_path.write_text(json.dumps(drifted), encoding="utf-8")
    with pytest.raises(FormalBuildIntegrityError, match="sampling_contract_sha256"):
        build_formal_shard(**kwargs, shard_id=0)
    with pytest.raises(FormalBuildIntegrityError, match="sampling_contract_sha256"):
        merge_formal_build_shards(**kwargs)
