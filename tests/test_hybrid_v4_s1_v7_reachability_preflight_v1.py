from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pytest

from scripts import hybrid_v3_atomic_policy_v2 as v2
from scripts import hybrid_v3_consistency_v3 as source_validator
from scripts import hybrid_v4_s1_v7_reachability_preflight_v1 as preflight
from tests.test_hybrid_v3_atomic_policy_v2 import _hypothesis, _packet


PASS_DATES = (
    "2023-06-01",
    "2023-07-03",
    "2023-08-01",
    "2023-09-01",
    "2023-10-02",
    "2024-02-02",
)


def _seal_packet(packet: dict[str, Any]) -> None:
    packet["question_manifest_sha256"] = preflight.canonical_sha256(
        packet["question_manifest"]
    )
    evidence_sha256 = preflight.canonical_sha256(packet["evidence"])
    packet["evidence_catalog_sha256"] = evidence_sha256
    packet["objective_facts"]["ai_visible_evidence_sha256"] = evidence_sha256
    core = dict(packet)
    core.pop("input_packet_sha256", None)
    packet["input_packet_sha256"] = preflight.canonical_sha256(core)


def _v7_mature_packet(index: int, as_of: str) -> dict[str, Any]:
    packet = _packet()
    mature = deepcopy(_hypothesis(packet, "MATURE_TREND_PULLBACK"))
    mature.update(
        {
            "taiji_generation": "COPY_LEG_5",
            "same_direction_attack_number": 3,
            "completed_prior_copy_count": 1,
        }
    )
    packet["review_id"] = f"D-{index + 1:024x}"
    packet["anonymous_stock_id"] = f"S-{index + 1:016x}"
    packet["as_of"] = as_of
    objective = packet["objective_facts"]
    objective.update(
        {
            "builder_version": preflight.EXPECTED_BUILDER_VERSION,
            "builder_status": preflight.EXPECTED_BUILDER_STATUS,
            "causal_cutoff_as_of": as_of,
            "performance_used_for_ordering_or_truncation": False,
            "relation_comparison_contract_version": (
                preflight.v7_builder.EVIDENCE_CONTRACT_VERSION
            ),
            "relation_comparison_evidence_count": 1,
            "scenario_hypotheses": {
                "MATURE_TREND_PULLBACK": [mature],
                "MACRO_COPY_RESONANCE": [],
                "BEAR_REVERSAL_LEFT_RIGHT": [],
                "FRESH_Q1_EXPANSION": [],
            },
        }
    )
    for scenario in (
        "MACRO_COPY_RESONANCE",
        "BEAR_REVERSAL_LEFT_RIGHT",
        "FRESH_Q1_EXPANSION",
    ):
        objective["data_sufficiency_by_route"][scenario].update(
            {
                "status": False,
                "reason_code": "INSUFFICIENT_ROUTE_OBJECTIVE_EVIDENCE",
            }
        )
    packet["evidence"].append(
        {
            "ref": f"RELATION_COMPARISON:C-{index + 1:020x}",
            "kind": "CAUSAL_RELATION_COMPARISON",
            "date": as_of,
            "values": {
                "contract_version": preflight.v7_builder.EVIDENCE_CONTRACT_VERSION,
                "scenario": "MATURE_TREND_PULLBACK",
                "hypothesis_id": mature["hypothesis_id"],
                "causal_cutoff_enforced": True,
                "pass_fail_or_trade_label_included": False,
            },
        }
    )
    packet["question_manifest"] = v2.expected_question_manifest(packet)
    _seal_packet(packet)
    return packet


def _source_rows(packets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contract = source_validator.load_sampling_contract(
        preflight.SOURCE_SAMPLING_CONTRACT
    )
    rows = []
    for ordinal, packet in enumerate(packets):
        eligible = source_validator.expected_eligible_sampling_strata(packet, contract)
        assert preflight.TARGET_SAMPLING_STRATUM in eligible
        rows.append(
            {
                "source_ordinal": ordinal,
                "review_id": packet["review_id"],
                "anonymous_stock_id": packet["anonymous_stock_id"],
                "eligible_sampling_strata": eligible,
                "eligible_sampling_strata_sha256": preflight.canonical_sha256(
                    eligible
                ),
                "primary_sampling_focus": preflight.TARGET_SAMPLING_STRATUM,
                "packet_sha256": preflight.canonical_sha256(packet),
                "packet": packet,
            }
        )
    return rows


def _write_rows_and_refresh_manifest(
    source: Path, manifest_path: Path, rows: list[dict[str, Any]]
) -> None:
    payload = b"".join(preflight.canonical_json_bytes(row) + b"\n" for row in rows)
    source.write_bytes(payload)
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else {}
    )
    row_count = len(rows)
    artifact_sha256 = preflight.file_sha256(source)
    source_index = [{key: value for key, value in row.items() if key != "packet"} for row in rows]
    manifest.update(
        {
            "builder_version": preflight.EXPECTED_BUILDER_VERSION,
            "builder_status": preflight.EXPECTED_BUILDER_STATUS,
            "builder_code_sha256": preflight.EXPECTED_V7_SHA256,
            "status": preflight.EXPECTED_MANIFEST_STATUS,
            "source_order_locked": False,
            "outcome_blind": True,
            "identity_visible": False,
            "future_data_visible": False,
            "performance_visible": False,
            "identity_isolated": True,
            "stocks": len({row["anonymous_stock_id"] for row in rows}),
            "stock_days_scanned": row_count,
            "as_of_ceiling": max(str(row["packet"]["as_of"]) for row in rows),
            "review_points": row_count,
            "expected_candidate3_review_points": row_count,
            "sampling_contract_sha256": preflight.EXPECTED_SAMPLING_CONTRACT_SHA256,
            "artifact_sha256": artifact_sha256,
            "review_points_artifact": {
                "path": str(source.resolve()),
                "sha256": artifact_sha256,
                "rows": row_count,
            },
            "source_ordinal_coverage": {
                "expected": row_count,
                "covered": row_count,
                "missing": 0,
                "overlap": 0,
                "unexpected": 0,
                "first": 0,
                "last": row_count - 1,
            },
            "source_ordinal_index_sha256": preflight.canonical_sha256(source_index),
            "review_ids_sha256": preflight.canonical_sha256(
                [row["review_id"] for row in rows]
            ),
            "packet_hashes_sha256": preflight.canonical_sha256(
                [row["packet_sha256"] for row in rows]
            ),
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _stage(tmp_path: Path, *, rows: int, start: str, end: str) -> Path:
    path = tmp_path / "stage.json"
    path.write_text(
        json.dumps(
            {
                "stage_protocol_version": preflight.EXPECTED_STAGE_VERSION,
                "status": "FINAL_RESEARCH_LOCKED",
                "target": {
                    "primary_scenario": preflight.TARGET_SCENARIO,
                    "trade_route": preflight.TARGET_ROUTE,
                    "other_v3_routes_suspended": True,
                },
                "rule_lineage": {
                    "v1_v2_v3_are_read_only": True,
                    "policy_version": preflight.EXPECTED_POLICY_VERSION,
                },
                "source": {
                    "stocks": rows,
                    "review_points": rows,
                    "monitoring_start": start,
                    "monitoring_end": end,
                    "outcome_blind": True,
                    "identity_visible": False,
                    "future_or_performance_visible": False,
                },
                "consistency_sample": {
                    "one_case_per_anonymous_stock": True,
                    "minimum_distinct_months": 6,
                    "quotas": {"MATURE_SYMBOLIC_REACHABLE": 18},
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dates: tuple[str, ...] = PASS_DATES,
) -> tuple[Path, Path, Path, Path, list[dict[str, Any]]]:
    packets = [_v7_mature_packet(index, dates[index % len(dates)]) for index in range(18)]
    rows = _source_rows(packets)
    source = tmp_path / "review_points.jsonl"
    manifest = tmp_path / "review_point_manifest.json"
    _write_rows_and_refresh_manifest(source, manifest, rows)
    start = min(dates)
    end = max(dates)
    stage = _stage(tmp_path, rows=len(rows), start=start, end=end)
    policy = preflight.POLICY_MODULE_PATH
    monkeypatch.setattr(preflight, "EXPECTED_STOCKS", len(rows))
    monkeypatch.setattr(preflight, "EXPECTED_STOCK_DAYS", len(rows))
    monkeypatch.setattr(preflight, "EXPECTED_MONITORING_START", start)
    monkeypatch.setattr(preflight, "EXPECTED_MONITORING_END", end)
    monkeypatch.setattr(preflight, "EXPECTED_STAGE_SHA256", preflight.file_sha256(stage))
    return source, manifest, policy, stage, rows


def _run(paths: tuple[Path, Path, Path, Path, list[dict[str, Any]]]) -> dict[str, Any]:
    source, manifest, policy, stage, _ = paths
    return preflight.preflight(
        source_path=source,
        source_manifest_path=manifest,
        policy_path=policy,
        stage_path=stage,
    )


def test_symbolic_witness_reuses_frozen_reducer_without_mutating_packet() -> None:
    packet = _v7_mature_packet(0, "2023-06-01")
    packet_before = deepcopy(packet)
    hypothesis = packet["objective_facts"]["scenario_hypotheses"][
        "MATURE_TREND_PULLBACK"
    ][0]
    symbolic = preflight.symbolic_witness(packet, hypothesis)
    assert symbolic["_preflight_symbolic_only"] == {
        "preflight_version": preflight.PREFLIGHT_VERSION,
        "classification": "NOT_AI_OUTPUT_NOT_COURSE_GOLD",
    }
    semantic = preflight._symbolic_validation_projection(symbolic)
    assert preflight.validate_atomic(packet, semantic) == []
    decision = preflight.reduce_atomic_v4_s1(packet, semantic)
    assert preflight.stage_permission_v4_s1(decision) == "TRADE"
    assert packet == packet_before


def test_preflight_passes_only_with_18_stocks_and_six_candidate_months(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    source_hash_before = preflight.file_sha256(paths[0])
    result = _run(paths)
    assert result["status"] == "PASS_SYMBOLIC_TARGET_ROUTE_REACHABLE"
    assert result["formal_acceptance_claimed"] is False
    assert result["capacity"] == {
        "reachable_rows": 18,
        "unique_anonymous_stocks": 18,
        "distinct_months": 6,
    }
    assert len(result["candidates"]) == 18
    assert all(
        row["sampling_stratum"] == "V2_CORE_OBJECTIVE_PROXY"
        and row["classification"] == "SYMBOLIC_REACHABLE_NOT_SEMANTIC_GOLD"
        and "symbolic_witness" not in row
        for row in result["candidates"]
    )
    assert preflight.file_sha256(paths[0]) == source_hash_before
    assert all(
        "_preflight_symbolic_only" not in row["packet"]
        and "symbolic_witness" not in row["packet"]
        for row in paths[4]
    )
    provenance = result["producer_provenance"]
    assert set(provenance) == {
        "preflight_builder",
        "v7_packet_builder",
        "merge_adapter",
        "v4_s1_policy",
        "stage_protocol",
        "source_validator",
        "source_sampling_contract",
    }
    assert provenance["v7_packet_builder"]["sha256"] == preflight.EXPECTED_V7_SHA256
    assert provenance["merge_adapter"]["version"] == preflight.EXPECTED_MERGE_ADAPTER_VERSION
    assert provenance["merge_adapter"]["sha256"] == preflight.file_sha256(
        preflight.MERGE_ADAPTER_PATH
    )


def test_five_candidate_months_do_not_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _fixture(
        tmp_path,
        monkeypatch,
        dates=("2023-06-01", "2023-07-03", "2023-08-01", "2023-09-01", "2024-02-02"),
    )
    result = _run(paths)
    assert result["status"] == "FAIL_INSUFFICIENT_SYMBOLIC_TARGET_ROUTE_CAPACITY"
    assert result["capacity"]["unique_anonymous_stocks"] == 18
    assert result["capacity"]["distinct_months"] == 5


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("builder_version", "hybrid-v3-atomic-packets-v6", "builder_version"),
        ("builder_status", "FINAL", "builder_status"),
        ("status", "LOCKED_OUTCOME_BLIND", "status"),
        ("source_order_locked", True, "source_order_locked"),
    ],
)
def test_candidate_manifest_contract_is_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
    message: str,
) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    manifest = json.loads(paths[1].read_text(encoding="utf-8"))
    manifest[field] = value
    paths[1].write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(preflight.V7ReachabilityPreflightError, match=message):
        _run(paths)


def test_source_artifact_hash_mismatch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    paths[0].write_bytes(paths[0].read_bytes() + b"\n")
    with pytest.raises(
        preflight.V7ReachabilityPreflightError,
        match="source artifact differs from manifest",
    ):
        _run(paths)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("ordinal", "source ordinal order changed"),
        ("packet_hash", "source contract invalid"),
        ("anonymous", "invalid anonymous stock id"),
        ("future_key", "identity/outcome/future/witness key"),
        ("future_evidence", "source contract invalid"),
        ("performance", "source contract invalid"),
        ("witness", "identity/outcome/future/witness key"),
    ],
)
def test_strict_row_packet_anonymity_and_causality_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    rows = paths[4]
    row = rows[0]
    packet = row["packet"]
    if mutation == "ordinal":
        row["source_ordinal"] = 999
    elif mutation == "packet_hash":
        row["packet_sha256"] = "0" * 64
    elif mutation == "anonymous":
        packet["anonymous_stock_id"] = "2330"
        row["anonymous_stock_id"] = "2330"
        _seal_packet(packet)
        row["packet_sha256"] = preflight.canonical_sha256(packet)
    elif mutation == "future_key":
        packet["future_return"] = 1.0
        _seal_packet(packet)
        row["packet_sha256"] = preflight.canonical_sha256(packet)
    elif mutation == "future_evidence":
        packet["evidence"].append(
            {
                "ref": "BAR:2099-01-01",
                "kind": "BAR",
                "date": "2099-01-01",
                "values": {"close": 1.0},
            }
        )
        _seal_packet(packet)
        row["packet_sha256"] = preflight.canonical_sha256(packet)
    elif mutation == "performance":
        packet["objective_facts"]["performance_used_for_ordering_or_truncation"] = True
        _seal_packet(packet)
        row["packet_sha256"] = preflight.canonical_sha256(packet)
    elif mutation == "witness":
        packet["symbolic_witness"] = {"leak": True}
        _seal_packet(packet)
        row["packet_sha256"] = preflight.canonical_sha256(packet)
    _write_rows_and_refresh_manifest(paths[0], paths[1], rows)
    with pytest.raises(preflight.V7ReachabilityPreflightError, match=message):
        _run(paths)


def test_only_v2_core_rows_are_sent_to_mature_reducer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _fixture(tmp_path, monkeypatch)
    for row in paths[4]:
        eligible = ["MACRO_COPY_OBJECTIVE_PROXY"]
        row["eligible_sampling_strata"] = eligible
        row["eligible_sampling_strata_sha256"] = preflight.canonical_sha256(eligible)
        row["primary_sampling_focus"] = eligible[0]
    _write_rows_and_refresh_manifest(paths[0], paths[1], paths[4])
    monkeypatch.setattr(preflight, "validate_source_record", lambda *_args: None)
    result = _run(paths)
    assert result["status"] == "FAIL_INSUFFICIENT_SYMBOLIC_TARGET_ROUTE_CAPACITY"
    assert result["capacity"]["reachable_rows"] == 0
    assert result["counts"]["rows_outside_v2_core"] == 18
    assert result["counts"].get("mature_v2_core_hypotheses_tested", 0) == 0


def test_publish_is_idempotent_and_rejects_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(_fixture(tmp_path, monkeypatch))
    output = tmp_path / "published"
    first = preflight.publish_report(result, output)
    second = preflight.publish_report(result, output)
    assert first == second
    assert set(first) == {"preflight.json", "preflight.md"}
    changed = dict(result)
    changed["status"] = "FAIL_CHANGED_FOR_CONFLICT_TEST"
    with pytest.raises(
        preflight.V7ReachabilityPreflightError,
        match="refusing to overwrite changed preflight",
    ):
        preflight.publish_report(changed, output)
